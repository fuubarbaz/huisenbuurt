"""CBS demographics provider tests — offline, against a fake OData client.

The padding test is the important one: an unpadded filter returns an empty
list with a 200, so this behaviour has no natural failure signal in production.
"""
from __future__ import annotations

import pytest

from app.models.enrichment import ProviderStatus
from app.models.property import GeoIdentity, ListingSource, PropertyListing
from app.services.enrichment.providers.base import EnrichmentContext
from app.services.enrichment.providers.cbs_demographics import (
    F_AGE_0_15,
    F_DIST_DAYCARE,
    F_HOUSEHOLD_SIZE,
    F_HOUSEHOLDS,
    F_HOUSEHOLDS_KIDS,
    F_POPULATION,
    CBSDemographicsProvider,
)
from app.services.enrichment.providers.cbs_odata import NATIONAL_CODE, clean, pad_area


def row(total, kids, pop=1000, age=100, size=2.1, daycare=0.4, **extra):
    base = {
        F_HOUSEHOLDS: total, F_HOUSEHOLDS_KIDS: kids, F_POPULATION: pop,
        F_AGE_0_15: age, F_HOUSEHOLD_SIZE: size, F_DIST_DAYCARE: daycare,
    }
    base.update(extra)
    return base


NATIONAL = row(8_430_352, 2_657_646)


class FakeClient:
    """Stands in for CBSODataClient, recording what was asked for."""

    dataset = "86165NED"

    def __init__(self, rows: dict):
        self.rows = rows
        self.requested: list[str] = []

    async def rows_for_areas(self, area_codes, select, extra_filter=None):
        self.requested = list(area_codes)
        return {c: self.rows[c] for c in area_codes if c in self.rows}

    @staticmethod
    def pct(n, d):
        return None if n is None or not d else round(100.0 * float(n) / float(d), 1)


def make_ctx(**geo_kw) -> EnrichmentContext:
    listing = PropertyListing(
        property_id="t1", source=ListingSource.MANUAL, url="https://e.invalid",
        address="Teststraat 1", postal_code="1015AA", house_number="1",
    )
    geo = GeoIdentity(latitude=52.3, longitude=4.9, **geo_kw)
    return EnrichmentContext(listing=listing, geo=geo)


def provider_with(rows) -> tuple[CBSDemographicsProvider, FakeClient]:
    p = CBSDemographicsProvider.__new__(CBSDemographicsProvider)
    p.http = None
    client = FakeClient(rows)
    p.client = client
    return p, client


async def test_buurt_level_is_preferred():
    rows = {"BU03620102": row(1745, 620), "WK036201": row(9000, 1000),
            "GM0362": row(40000, 5000), NATIONAL_CODE: NATIONAL}
    p, _ = provider_with(rows)
    data = await p.fetch(make_ctx(buurtcode="BU03620102", wijkcode="WK036201", gemeentecode="GM0362"))

    assert data.area_level == "buurt"
    assert data.buurtcode == "BU03620102"
    assert data.households_with_children_pct == pytest.approx(35.5, abs=0.1)
    # 35.5% against a 31.5% national share.
    assert data.children_pct_vs_national == pytest.approx(1.13, abs=0.02)


async def test_falls_back_to_wijk_when_buurt_is_suppressed():
    rows = {"BU03620102": row(None, None), "WK036201": row(9000, 3000),
            "GM0362": row(40000, 5000), NATIONAL_CODE: NATIONAL}
    p, _ = provider_with(rows)
    data = await p.fetch(make_ctx(buurtcode="BU03620102", wijkcode="WK036201", gemeentecode="GM0362"))

    assert data.area_level == "wijk"
    assert data.households_with_children_pct == pytest.approx(33.3, abs=0.1)


async def test_falls_back_to_gemeente_when_buurt_and_wijk_are_absent():
    rows = {"GM0362": row(40000, 9120), NATIONAL_CODE: NATIONAL}
    p, _ = provider_with(rows)
    data = await p.fetch(make_ctx(buurtcode="BU03620102", wijkcode="WK036201", gemeentecode="GM0362"))

    assert data.area_level == "gemeente"


async def test_whole_chain_requested_in_one_call():
    """The fallback chain plus the national baseline must cost one round-trip."""
    rows = {"BU03620102": row(1745, 620), NATIONAL_CODE: NATIONAL}
    p, client = provider_with(rows)
    await p.fetch(make_ctx(buurtcode="BU03620102", wijkcode="WK036201", gemeentecode="GM0362"))

    assert client.requested == ["BU03620102", "WK036201", "GM0362", NATIONAL_CODE]


async def test_missing_everything_is_reported_not_raised():
    p, _ = provider_with({NATIONAL_CODE: NATIONAL})
    result = await p.run(make_ctx(buurtcode="BU99999999"), timeout=5)

    assert result.status is ProviderStatus.MISSING
    assert not result.usable


async def test_no_area_code_at_all():
    p, _ = provider_with({})
    result = await p.run(make_ctx(), timeout=5)

    assert result.status is ProviderStatus.MISSING
    assert "no CBS area code" in result.error


# -- the two CBS traps -------------------------------------------------------


def test_area_codes_are_padded_to_ten_chars():
    # Unpadded filters return an empty 200 from CBS, so this is silent in prod.
    assert pad_area("GM0363") == "GM0363    "
    assert pad_area("WK0363AC") == "WK0363AC  "
    assert pad_area("BU0363AC01") == "BU0363AC01"
    assert len(pad_area("NL00")) == 10


def test_string_values_are_unpadded_on_the_way_back():
    assert clean("Amsterdam      ") == "Amsterdam"
    assert clean(1885) == 1885
    assert clean(None) is None


# -- the wider demographic picture -------------------------------------------

from app.services.enrichment.providers.cbs_demographics import (  # noqa: E402
    AGE_BANDS,
    F_AGE_15_25,
    F_AGE_25_45,
    F_AGE_45_65,
    F_AGE_65_PLUS,
    F_NO_CHILDREN,
    F_OWNER_OCCUPIED,
    F_RENTAL,
    F_SINGLE_FAMILY,
    F_SINGLE_PERSON,
    F_SOCIAL_HOUSING,
)

# Amstelveen BU03620102, as CBS actually publishes it.
RICH = row(1745, 620, pop=3720, age=675, size=2.1, **{
    F_AGE_15_25: 410, F_AGE_25_45: 1190, F_AGE_45_65: 875, F_AGE_65_PLUS: 565,
    F_SINGLE_PERSON: 745, F_NO_CHILDREN: 380,
    F_OWNER_OCCUPIED: 66, F_RENTAL: 34, F_SOCIAL_HOUSING: 0,
    F_SINGLE_FAMILY: 44,
})


async def test_age_counts_become_shares_of_the_population():
    """Shares, so one area compares with another regardless of size."""
    p, _ = provider_with({"BU1": RICH, NATIONAL_CODE: NATIONAL})
    data = await p.fetch(make_ctx(buurtcode="BU1"))

    assert set(data.age_bands_pct) == {b for b, _ in AGE_BANDS}
    assert data.age_bands_pct["0-15"] == pytest.approx(18.1, abs=0.1)
    assert data.age_bands_pct["65+"] == pytest.approx(15.2, abs=0.1)
    assert sum(data.age_bands_pct.values()) == pytest.approx(100, abs=1.0)


async def test_household_composition_is_reported_as_shares():
    p, _ = provider_with({"BU1": RICH, NATIONAL_CODE: NATIONAL})
    data = await p.fetch(make_ctx(buurtcode="BU1"))

    assert data.single_person_households_pct == pytest.approx(42.7, abs=0.1)
    assert data.households_without_children_pct == pytest.approx(21.8, abs=0.1)


async def test_tenure_and_stock_pass_through_as_published_percentages():
    p, _ = provider_with({"BU1": RICH, NATIONAL_CODE: NATIONAL})
    data = await p.fetch(make_ctx(buurtcode="BU1"))

    assert data.owner_occupied_pct == 66
    assert data.rental_pct == 34
    assert data.social_housing_pct == 0        # zero is a figure, not a gap
    assert data.single_family_homes_pct == 44


async def test_absent_bands_are_omitted_rather_than_zeroed():
    """A suppressed band must not read as "nobody of that age lives here"."""
    sparse = row(1745, 620, pop=3720, age=675)      # only the 0-15 band present
    p, _ = provider_with({"BU1": sparse, NATIONAL_CODE: NATIONAL})
    data = await p.fetch(make_ctx(buurtcode="BU1"))

    assert set(data.age_bands_pct) == {"0-15"}


async def test_no_population_means_no_age_shares():
    p, _ = provider_with({"BU1": row(1745, 620, pop=None), NATIONAL_CODE: NATIONAL})
    data = await p.fetch(make_ctx(buurtcode="BU1"))

    assert data.age_bands_pct == {}
