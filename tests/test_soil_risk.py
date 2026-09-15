"""Soil / foundation risk provider tests — offline.

The interesting logic is the interaction between ground vulnerability and the
building's own era: either alone is not the hazard.
"""
from __future__ import annotations

import pytest

from app.models.enrichment import ProviderStatus
from app.models.property import GeoIdentity, ListingSource, PropertyListing
from app.services.enrichment.providers.base import EnrichmentContext
from app.services.enrichment.providers.soil_risk import (
    FOUNDATION_ERA_CUTOFF,
    HIGH_RISK_SOIL_REGIONS,
    VULNERABLE_URBAN_PROVINCES,
    SoilRiskProvider,
)


def row(legenda, fgr="Niet indeelbaar", provincie="Noord-Holland",
        pc6="1015AA", pre1970=83.3, nbag=6):
    return {"legenda": legenda, "fgr": fgr, "provincie": provincie,
            "pc6": pc6, "percvoor1970": pre1970, "nBag": nbag}


URBAN_NH = row("Stedelijk gebied - 80-100 %")
URBAN_LIMBURG = row("Stedelijk gebied - 80-100 %", provincie="Limburg", pc6="6211AA")
SAND = row("Niet kwetsbaar gebied - 80-100 %", fgr="Hogere Zandgronden",
           provincie="Gelderland", pc6="7311AA")
PEAT = row("Kwetsbaar gebied - 60-80 %", fgr="Laagveengebied",
           provincie="Zuid-Holland", pc6="2801AA")


class FakeWFS:
    def __init__(self, rows): self.rows, self.calls = rows, 0

    async def features_at(self, type_name, **kw):
        self.calls += 1
        value = self.rows
        if isinstance(value, Exception):
            raise value
        return value


class FakeFloodWMS:
    """Mirrors the shape test_rivm_air.py uses for the same GRAY_INDEX
    raster convention — this layer just happens to be a categorical one."""

    def __init__(self, value):
        self.value = value

    async def feature_info(self, layer, **kw):
        if isinstance(self.value, Exception):
            raise self.value
        if self.value is None:
            return {}
        return {"GRAY_INDEX": self.value}


def build(foundation_rows, flood_value=None):
    p = SoilRiskProvider.__new__(SoilRiskProvider)
    p.http = None
    p.foundation = FakeWFS(foundation_rows)
    p.flood = FakeFloodWMS(flood_value)
    return p


def ctx(year=None, postal_code="1015AA"):
    listing = PropertyListing(
        property_id="s1", source=ListingSource.MANUAL, url="https://e.invalid",
        address="Teststraat 1", postal_code=postal_code, house_number="1",
        construction_year=year,
    )
    return EnrichmentContext(
        listing=listing,
        geo=GeoIdentity(latitude=52.378, longitude=4.893, rd_x=121359.0, rd_y=487957.0),
    )


# -- the age/ground interaction ---------------------------------------------


async def test_same_postcode_diverges_on_construction_year():
    """The whole point: vulnerable ground only matters under an old foundation."""
    old = await build([URBAN_NH]).fetch(ctx(year=1890))
    new = await build([URBAN_NH]).fetch(ctx(year=2015))

    assert old.paalrot_risk is True
    assert new.paalrot_risk is False
    assert old.foundation_risk_ordinal > new.foundation_risk_ordinal


async def test_old_house_on_sand_is_not_at_risk():
    data = await build([SAND]).fetch(ctx(year=1935, postal_code="7311AA"))

    assert data.paalrot_risk is False
    assert data.foundation_risk_class == "laag"


async def test_peat_is_the_worst_case():
    peat = await build([PEAT]).fetch(ctx(year=1935, postal_code="2801AA"))
    urban = await build([URBAN_NH]).fetch(ctx(year=1935))

    assert peat.foundation_risk_ordinal == 4
    assert peat.foundation_risk_class == "zeer hoog"
    assert peat.foundation_risk_ordinal > urban.foundation_risk_ordinal


def test_era_cutoff_is_1970():
    assert FOUNDATION_ERA_CUTOFF == 1970


# -- province stands in for missing urban soil classification ----------------


async def test_urban_risk_depends_on_province():
    """Cities have no soil class; the dataset says to judge them by region."""
    amsterdam = await build([URBAN_NH]).fetch(ctx(year=1935))
    maastricht = await build([URBAN_LIMBURG]).fetch(ctx(year=1935, postal_code="6211AA"))

    assert amsterdam.paalrot_risk is True
    assert maastricht.paalrot_risk is False


async def test_overig_class_is_judged_on_soil_not_assumed_safe():
    """"Overig" states no verdict, so the ground has to decide."""
    soft = row("Overig - 60-80 %", fgr="Zeekleigebied", provincie="Zuid-Holland")
    firm = row("Overig - 60-80 %", fgr="Hogere Zandgronden", provincie="Gelderland")

    assert (await build([soft]).fetch(ctx(year=1955))).paalrot_risk is True
    assert (await build([firm]).fetch(ctx(year=1955))).paalrot_risk is False


async def test_tidal_and_estuary_ground_counts_as_soft():
    for region in ("Getijdengebied", "Afgesloten Zeearmen", "Laagveengebied"):
        data = await build([row("Overig - 60-80 %", fgr=region)]).fetch(ctx(year=1955))
        assert data.paalrot_risk is True, region


def test_firm_ground_is_never_high_risk():
    for region in ("Hogere Zandgronden", "Heuvelland"):
        assert region.lower() not in HIGH_RISK_SOIL_REGIONS


def test_vulnerable_provinces_are_west_and_north():
    assert {"noord-holland", "zuid-holland", "utrecht", "groningen"} <= VULNERABLE_URBAN_PROVINCES
    assert "limburg" not in VULNERABLE_URBAN_PROVINCES
    assert "noord-brabant" not in VULNERABLE_URBAN_PROVINCES


# -- fallbacks and resilience ------------------------------------------------


async def test_unknown_year_falls_back_to_the_area_mix():
    data = await build([URBAN_NH]).fetch(ctx(year=None))

    assert data.construction_year_known is False
    # 83% of this postcode predates 1970, so the area reads as old.
    assert data.paalrot_risk is True


async def test_unknown_year_in_a_mostly_new_area_reads_as_new():
    data = await build([row("Stedelijk gebied - 0-20 %", pre1970=12.0)]).fetch(ctx(year=None))

    assert data.construction_year_known is False
    assert data.paalrot_risk is False


async def test_matching_postcode_wins_over_a_clipped_neighbour():
    neighbour = row("Kwetsbaar gebied - 80-100 %", pc6="1015ZZ")
    data = await build([neighbour, URBAN_NH]).fetch(ctx(year=1890, postal_code="1015AA"))

    assert data.area_postcode == "1015AA"


async def test_flood_failure_does_not_lose_the_foundation_verdict():
    p = build([URBAN_NH], flood_value=RuntimeError("flood service down"))
    data = await p.fetch(ctx(year=1890))

    assert data.foundation_risk_class == "hoog"
    assert data.flood_risk_ordinal is None


async def test_a_flood_class_is_recorded_with_its_label():
    data = await build([SAND], flood_value=4).fetch(ctx(year=1935, postal_code="7311AA"))

    assert data.flood_risk_ordinal == 4
    assert data.flood_risk_class == "1x per 100 jaar"


async def test_does_not_flood_is_the_best_class_not_absence():
    data = await build([SAND], flood_value=1).fetch(ctx(year=1935, postal_code="7311AA"))

    assert data.flood_risk_ordinal == 1
    assert data.flood_risk_class == "overstroomt niet"


async def test_open_water_is_reported_as_absence_not_a_flood_class():
    """Category 6 means the sample point landed on water, not that the
    address floods worse than the 1-in-10-year class."""
    data = await build([SAND], flood_value=6).fetch(ctx(year=1935, postal_code="7311AA"))

    assert data.flood_risk_ordinal is None
    assert data.flood_risk_class is None


async def test_the_nodata_sentinel_is_absence_too():
    data = await build([SAND], flood_value=-1).fetch(ctx(year=1935, postal_code="7311AA"))
    assert data.flood_risk_ordinal is None


async def test_no_polygon_is_missing_not_an_error():
    result = await build([]).run(ctx(year=1890), timeout=5)

    assert result.status is ProviderStatus.MISSING
    assert "no foundation-risk polygon" in result.error


async def test_foundation_service_failure_is_reported():
    result = await build(RuntimeError("pdok down")).run(ctx(year=1890), timeout=5)

    assert result.status is ProviderStatus.MISSING
    assert "foundation layer unavailable" in result.error


# -- parsing -----------------------------------------------------------------


def test_legenda_splits_into_class_and_age_band():
    split = SoilRiskProvider._split_legenda
    assert split("Kwetsbaar gebied - 40-60 %") == ("kwetsbaar gebied", "40-60 %")
    assert split("Niet kwetsbaar gebied - 0-20 %") == ("niet kwetsbaar gebied", "0-20 %")
    assert split(None) == (None, None)
    assert split("Onbekend") == ("onbekend", None)


async def test_unparseable_legenda_yields_no_verdict():
    data = await build([row(None)]).fetch(ctx(year=1890))

    assert data.foundation_risk_ordinal is None
    assert data.paalrot_risk is None


async def test_unavailable_sources_stay_none_rather_than_zero():
    """Fields with no reachable service must not read as measured zeros."""
    data = await build([URBAN_NH]).fetch(ctx(year=1890))

    assert data.subsidence_mm_per_year is None
    assert data.flood_depth_m is None
    assert data.contamination_status is None
