"""Politie provider tests — offline, against a stub client.

Covers the aggregation logic (12-month sums, ratios, trend) and the two CBS
padding widths, which differ per dimension and fail silently when wrong.
"""
from __future__ import annotations

import pytest

from app.models.enrichment import ProviderStatus
from app.models.property import GeoIdentity, ListingSource, PropertyListing
from app.services.enrichment.providers.base import EnrichmentContext
from app.services.enrichment.providers.cbs_odata import (
    AREA_KEY_WIDTH,
    CRIME_KEY_WIDTH,
    NATIONAL_CODE,
    any_of,
    pad,
)
from app.services.enrichment.providers.politie import (
    D_AREA,
    D_CRIME,
    F_COUNT,
    D_PERIOD,
    OFFENCE_GROUPS,
    TOTAL_CODE,
    PolitieProvider,
)

MONTHS_24 = [f"{y}MM{m:02d}" for y in (2025, 2026) for m in range(1, 13)]
CURRENT = MONTHS_24[-12:]
PREVIOUS = MONTHS_24[:12]


class FakeCrimeClient:
    """Serves a scripted counts table and records the filter it was given."""

    def __init__(self, per_month: dict[tuple[str, str], int]):
        self.per_month = per_month
        self.last_filter: str | None = None

    async def dimension_keys(self, dimension):
        return list(MONTHS_24)

    async def query(self, *, filter, select=None, top=5000):
        self.last_filter = filter
        rows = []
        for (area, code), value in self.per_month.items():
            for period in MONTHS_24:
                rows.append({D_AREA: area, D_CRIME: code, D_PERIOD: period, F_COUNT: value})
        return rows


class FakePopClient:
    def __init__(self, pops):
        self.pops = pops

    async def rows_for_areas(self, areas, select, extra_filter=None):
        return {a: {"AantalInwoners_5": self.pops[a]} for a in areas if a in self.pops}


def build(per_month, pops):
    p = PolitieProvider.__new__(PolitieProvider)
    p.http = None
    p.client = FakeCrimeClient(per_month)
    p.kwb = FakePopClient(pops)
    return p


def ctx(**geo):
    listing = PropertyListing(
        property_id="c1", source=ListingSource.MANUAL, url="https://e.invalid",
        address="Teststraat 1", postal_code="1015AA", house_number="1",
    )
    return EnrichmentContext(listing=listing, geo=GeoIdentity(latitude=52.0, longitude=4.9, **geo))


async def test_rates_are_twelve_month_sums_per_1000():
    # 2 burglaries a month in a 10,000-person buurt = 24/yr = 2.4 per 1000.
    per_month = {("BU1", "1.1.1"): 2, ("BU1", TOTAL_CODE): 10, (NATIONAL_CODE, TOTAL_CODE): 10}
    data = await build(per_month, {"BU1": 10_000, NATIONAL_CODE: 10_000}).fetch(ctx(buurtcode="BU1"))

    assert data.burglaries_per_1000 == pytest.approx(2.4)
    assert data.total_registered_per_1000 == pytest.approx(12.0)
    assert data.period == f"{CURRENT[0]}..{CURRENT[-1]}"


async def test_ratio_compares_like_with_like():
    """A group is compared against its own national rate, not against total."""
    per_month = {
        ("BU1", "1.1.1"): 4, ("BU1", TOTAL_CODE): 10,
        (NATIONAL_CODE, "1.1.1"): 2, (NATIONAL_CODE, TOTAL_CODE): 10,
    }
    data = await build(per_month, {"BU1": 1_000, NATIONAL_CODE: 1_000}).fetch(ctx(buurtcode="BU1"))

    assert data.category_ratios["burglaries"] == pytest.approx(2.0)
    assert data.national_average_ratio == pytest.approx(1.0)


async def test_absolute_counts_are_kept_alongside_the_rates():
    """A rate answers "compared to where?"; a count answers "what happened?"."""
    per_month = {("BU1", "1.1.1"): 2, ("BU1", "2.2.1"): 1,
                 ("BU1", TOTAL_CODE): 10, (NATIONAL_CODE, TOTAL_CODE): 10}
    data = await build(per_month, {"BU1": 10_000, NATIONAL_CODE: 10_000}).fetch(ctx(buurtcode="BU1"))

    assert data.counts_12m["burglaries"] == 24        # 2 a month for a year
    assert data.counts_12m["vandalism"] == 12


async def test_the_monthly_series_covers_the_window_oldest_first():
    per_month = {("BU1", TOTAL_CODE): 3, (NATIONAL_CODE, TOTAL_CODE): 3}
    data = await build(per_month, {"BU1": 1_000, NATIONAL_CODE: 1_000}).fetch(ctx(buurtcode="BU1"))

    assert len(data.monthly_totals) == 12
    assert [m.period for m in data.monthly_totals] == CURRENT
    assert data.monthly_totals[0].label == "2026-01"
    assert all(m.count == 3 for m in data.monthly_totals)


async def test_a_group_with_no_rows_is_absent_from_the_counts():
    """Absent must not render as a confident zero."""
    per_month = {("BU1", TOTAL_CODE): 5, (NATIONAL_CODE, TOTAL_CODE): 5}
    data = await build(per_month, {"BU1": 1_000, NATIONAL_CODE: 1_000}).fetch(ctx(buurtcode="BU1"))

    assert "burglaries" not in data.counts_12m


async def test_flat_series_has_no_trend():
    per_month = {("BU1", TOTAL_CODE): 5, (NATIONAL_CODE, TOTAL_CODE): 5}
    data = await build(per_month, {"BU1": 1_000, NATIONAL_CODE: 1_000}).fetch(ctx(buurtcode="BU1"))

    assert data.trend_12m_pct == pytest.approx(0.0)


async def test_falls_back_when_buurt_has_no_rows():
    per_month = {("GM1", TOTAL_CODE): 5, (NATIONAL_CODE, TOTAL_CODE): 5}
    data = await build(per_month, {"GM1": 1_000, NATIONAL_CODE: 1_000}).fetch(
        ctx(buurtcode="BU1", wijkcode="WK1", gemeentecode="GM1")
    )

    assert data.area_code == "GM1"
    assert data.area_level == "gemeente"


async def test_missing_population_is_reported_not_guessed():
    """Without a denominator a rate would be meaningless, so we decline."""
    per_month = {("BU1", TOTAL_CODE): 5, (NATIONAL_CODE, TOTAL_CODE): 5}
    result = await build(per_month, {}).run(ctx(buurtcode="BU1"), timeout=5)

    assert result.status is ProviderStatus.MISSING
    assert "population" in result.error


async def test_no_area_code():
    result = await build({}, {}).run(ctx(), timeout=5)
    assert result.status is ProviderStatus.MISSING


async def test_filter_uses_the_right_padding_width_per_dimension():
    per_month = {("BU1", TOTAL_CODE): 1, (NATIONAL_CODE, TOTAL_CODE): 1}
    provider = build(per_month, {"BU1": 1_000, NATIONAL_CODE: 1_000})
    await provider.fetch(ctx(buurtcode="BU1"))

    filt = provider.client.last_filter
    # Areas pad to 10, offence codes to 6. Wrong widths return an empty 200.
    assert f"{D_AREA} eq '{'BU1'.ljust(AREA_KEY_WIDTH)}'" in filt
    assert f"{D_CRIME} eq '{TOTAL_CODE.ljust(CRIME_KEY_WIDTH)}'" in filt


def test_offence_codes_are_padded_to_six_not_ten():
    assert pad("1.1.1", CRIME_KEY_WIDTH) == "1.1.1 "
    assert pad("2.6.10", CRIME_KEY_WIDTH) == "2.6.10"
    assert any_of("SoortMisdrijf", ["1.1.1"], CRIME_KEY_WIDTH) == "(SoortMisdrijf eq '1.1.1 ')"


def test_offence_groups_have_no_overlap():
    """A code in two groups would be double-counted in the safety blend."""
    seen: set[str] = set()
    for codes in OFFENCE_GROUPS.values():
        assert not (seen & set(codes))
        seen |= set(codes)
