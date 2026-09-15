"""BAG / EP-Online building-fact tests — offline."""
from __future__ import annotations

import pytest

from app.core.config import settings
from app.models.property import GeoIdentity, ListingSource, PropertyListing
from app.services.building import (
    PAND,
    VERBLIJFSOBJECT,
    BuildingLookup,
    fill_listing,
)

GEO = GeoIdentity(latitude=52.378, longitude=4.893, rd_x=121359.0, rd_y=487957.0)

# Singel 30, Amsterdam: nine dwellings inside one 18th-century canal house.
SINGEL_PAND = {"bouwjaar": 1730, "status": "Pand in gebruik", "gebruiksdoel": "woonfunctie",
               "oppervlakte_min": 1, "oppervlakte_max": 360, "aantal_verblijfsobjecten": 9}
SINGEL_UNIT = {"bouwjaar": 1702, "oppervlakte": 317, "gebruiksdoel": "woonfunctie"}

# A single-family house: pand and unit agree.
HOUSE_PAND = {"bouwjaar": 1930, "status": "Pand in gebruik", "oppervlakte_min": 162,
              "oppervlakte_max": 162, "aantal_verblijfsobjecten": 1}
HOUSE_UNIT = {"bouwjaar": 1930, "oppervlakte": 162}


class FakeWFS:
    def __init__(self, by_type, by_id=None):
        self.by_type, self.by_id = by_type, by_id or {}
        self.id_lookups = []

    async def features_at(self, type_name, **kw):
        value = self.by_type.get(type_name)
        if isinstance(value, Exception):
            raise value
        return [value] if value else []

    async def feature_by_id(self, type_name, *, field, value):
        self.id_lookups.append(value)
        return self.by_id.get(value)


def lookup(pand=None, unit=None, by_id=None) -> BuildingLookup:
    b = BuildingLookup.__new__(BuildingLookup)
    b.http = None
    b.bag = FakeWFS({PAND: pand, VERBLIJFSOBJECT: unit}, by_id)
    return b


def listing(**kw) -> PropertyListing:
    base = dict(property_id="p1", source=ListingSource.MANUAL, url="https://e.invalid",
                address="Singel 30", postal_code="1015AA", house_number="30")
    base.update(kw)
    return PropertyListing(**base)


@pytest.fixture(autouse=True)
def no_energy_key(monkeypatch):
    monkeypatch.setattr(settings, "eponline_api_key", None)


# -- which year to believe ---------------------------------------------------


async def test_the_building_year_is_used_not_the_unit_year():
    """The foundation belongs to the structure, so the pand year governs risk."""
    facts = await lookup(SINGEL_PAND, SINGEL_UNIT).for_location(GEO)

    assert facts.construction_year == 1730
    assert facts.unit_construction_year == 1702


async def test_bag_placeholder_years_are_discarded():
    """BAG uses 1005 for "old, but unknown" — not a real date."""
    facts = await lookup({**HOUSE_PAND, "bouwjaar": 1005}).for_location(GEO)
    assert facts.construction_year is None


# -- picking the right dwelling ----------------------------------------------

# Cruquiuskade 289 in Amsterdam: 289 is 183 m², 289-A is 37 m², 289-B is 76 m².
# A spatial query cannot tell them apart; the BAG id can.
FLAT_B = {"identificatie": "0363010012598954", "oppervlakte": 76, "bouwjaar": 2002}
BLOCK_PAND = {"bouwjaar": 2002, "aantal_verblijfsobjecten": 350,
              "oppervlakte_min": 30, "oppervlakte_max": 400, "status": "Pand in gebruik"}


async def test_the_dwelling_id_selects_the_right_flat():
    """350 dwellings share this footprint, so the id is the only way through."""
    geo = GeoIdentity(latitude=52.37, longitude=4.93, rd_x=1.0, rd_y=2.0,
                      bag_verblijfsobject_id="0363010012598954")
    facts = await lookup(BLOCK_PAND, SINGEL_UNIT,
                         by_id={"0363010012598954": FLAT_B}).for_location(geo)

    assert facts.floor_area_m2 == 76            # not the neighbour's 317
    assert facts.floor_area_is_exact is True


async def test_without_an_id_a_shared_footprint_yields_no_exact_area():
    facts = await lookup(BLOCK_PAND, SINGEL_UNIT).for_location(GEO)
    assert facts.floor_area_is_exact is False


async def test_an_id_that_resolves_to_nothing_falls_back_to_the_spatial_query():
    geo = GeoIdentity(latitude=52.37, longitude=4.93, rd_x=1.0, rd_y=2.0,
                      bag_verblijfsobject_id="does-not-exist")
    facts = await lookup(HOUSE_PAND, HOUSE_UNIT, by_id={}).for_location(geo)

    assert facts.floor_area_m2 == 162           # recovered the other way


# -- floor area, and when not to claim one -----------------------------------


async def test_a_single_dwelling_reports_its_exact_area():
    facts = await lookup(HOUSE_PAND, HOUSE_UNIT).for_location(GEO)

    assert facts.floor_area_m2 == 162
    assert facts.floor_area_is_exact is True


async def test_a_block_of_flats_does_not_claim_a_unit_area():
    """With nine dwellings in the footprint we cannot tell which flat it is."""
    facts = await lookup(SINGEL_PAND, SINGEL_UNIT).for_location(GEO)

    assert facts.units_in_building == 9
    assert facts.floor_area_is_exact is False


async def test_a_failing_bag_call_is_not_fatal():
    """Losing the pand loses the unit count, so the unit area is not trusted."""
    facts = await lookup(RuntimeError("pdok down"), HOUSE_UNIT).for_location(GEO)

    assert facts.construction_year is None
    assert facts.floor_area_is_exact is False
    assert fill_listing(listing(), facts).living_area_m2 is None


async def test_no_building_found_yields_empty_facts():
    facts = await lookup().for_location(GEO)
    assert facts.has_anything is False


# -- filling gaps ------------------------------------------------------------


async def test_a_blank_listing_is_filled():
    facts = await lookup(HOUSE_PAND, HOUSE_UNIT).for_location(GEO)
    filled = fill_listing(listing(), facts)

    assert filled.construction_year == 1930
    assert filled.living_area_m2 == 162


async def test_what_the_listing_states_always_wins():
    """The seller knows about the converted attic; the registry does not."""
    facts = await lookup(HOUSE_PAND, HOUSE_UNIT).for_location(GEO)
    filled = fill_listing(listing(construction_year=1890, living_area_m2=200), facts)

    assert filled.construction_year == 1890
    assert filled.living_area_m2 == 200


async def test_an_inexact_area_is_never_used_to_fill():
    facts = await lookup(SINGEL_PAND, SINGEL_UNIT).for_location(GEO)
    filled = fill_listing(listing(), facts)

    assert filled.construction_year == 1730     # the year is still good
    assert filled.living_area_m2 is None        # the area is not


async def test_filling_nothing_returns_the_same_object():
    facts = await lookup().for_location(GEO)
    original = listing()
    assert fill_listing(original, facts) is original


# -- energy label ------------------------------------------------------------


async def test_no_api_key_means_no_label_not_an_error():
    facts = await lookup(HOUSE_PAND, HOUSE_UNIT).for_location(
        GEO, postal_code="1015AA", house_number="30")

    assert facts.energy_label is None
    assert facts.construction_year == 1930      # the rest still resolved


# The register's documented field names — not the ones a reasonable person
# would guess, which is why this fixture is spelled out.
REGISTER_RECORD = {
    "Energieklasse": "C",
    "Geldig_tot": "2033-01-01",
    "Registratiedatum": "2023-01-01",
    "Gebouwtype": "Tussenwoning",
}

# A real registration, read off EP-Online for Kwadijkerpark 45, 1444JE
# Purmerend. Kept verbatim so the parsing is pinned to data the register
# actually returns rather than to what its schema suggested.
REAL_RECORD = {
    "Energieklasse": "A+++",
    "Status": "Oplevering",
    "Registratiedatum": "2025-11-17",
    "Opnamedatum": "2025-10-22",
    "Geldig_tot": "2035-10-22",
    "Certificaathouder": "BengCert",
    "Soort_opname": "Detailopname",
    "Berekeningstype": "NTA 8800:2024",
    "BAGVerblijfsobjectID": "0439010000206342",
    "BAGPandIDs": ["0439100000213253"],
    "Gebruiksoppervlakte_thermische_zone": 132.32,
    "Gebouwklasse": "Woningbouw",
}


async def test_a_real_registration_parses(monkeypatch):
    """Kwadijkerpark 45: PDOK returns the same BAG id EP-Online indexes on."""
    monkeypatch.setattr(settings, "eponline_api_key", "test-key")
    b = lookup(HOUSE_PAND, HOUSE_UNIT)

    class FakeHttp:
        async def get_json(self, url, **kw):
            assert url.endswith("/AdresseerbaarObject/0439010000206342")
            return [REAL_RECORD]

    b.http = FakeHttp()
    geo = GeoIdentity(latitude=52.5, longitude=4.95, rd_x=1.0, rd_y=2.0,
                      bag_verblijfsobject_id="0439010000206342")
    facts = await b.for_location(geo, postal_code="1444JE", house_number="45")

    assert facts.energy_label == "A+++"
    assert facts.energy_label_valid_until == "2035-10-22"
    assert facts.energy_label_registered == "2025-11-17"
    # No Gebouwtype on this record; the house type must stay absent, not guessed.
    assert facts.house_type is None


async def test_the_label_is_read_when_a_key_is_set(monkeypatch):
    monkeypatch.setattr(settings, "eponline_api_key", "test-key")
    b = lookup(HOUSE_PAND, HOUSE_UNIT)

    class FakeHttp:
        async def get_json(self, url, **kw):
            assert kw["headers"]["Authorization"] == "test-key"
            return [REGISTER_RECORD]

    b.http = FakeHttp()
    facts = await b.for_location(GEO, postal_code="1015AA", house_number="30")

    assert facts.energy_label == "C"
    assert facts.energy_label_valid_until == "2033-01-01"
    assert facts.energy_label_registered == "2023-01-01"


async def test_the_register_also_supplies_the_house_type(monkeypatch):
    """Gebouwtype is what the renovation cost tables are keyed on."""
    monkeypatch.setattr(settings, "eponline_api_key", "test-key")
    b = lookup(HOUSE_PAND, HOUSE_UNIT)

    class FakeHttp:
        async def get_json(self, url, **kw):
            return [REGISTER_RECORD]

    b.http = FakeHttp()
    facts = await b.for_location(GEO, postal_code="1015AA", house_number="30")

    assert facts.house_type == "terraced"


async def test_the_bag_id_is_preferred_over_postcode(monkeypatch):
    """One dwelling exactly, where a postcode can match several flats."""
    monkeypatch.setattr(settings, "eponline_api_key", "test-key")
    b = lookup(HOUSE_PAND, HOUSE_UNIT)
    urls = []

    class FakeHttp:
        async def get_json(self, url, **kw):
            urls.append(url)
            return [REGISTER_RECORD]

    b.http = FakeHttp()
    geo = GeoIdentity(latitude=52.3, longitude=4.9, rd_x=1.0, rd_y=2.0,
                      bag_verblijfsobject_id="0363010000809684")
    await b.for_location(geo, postal_code="1015AA", house_number="30")

    assert urls[0].endswith("/AdresseerbaarObject/0363010000809684")
    assert not any("/Adres?" in u or u.endswith("/Adres") for u in urls)


async def test_no_registration_for_the_bag_id_falls_back_to_the_address(monkeypatch):
    monkeypatch.setattr(settings, "eponline_api_key", "test-key")
    b = lookup(HOUSE_PAND, HOUSE_UNIT)
    urls = []

    class FakeHttp:
        async def get_json(self, url, **kw):
            urls.append(url)
            if "AdresseerbaarObject" in url:
                raise RuntimeError("404 no registration")
            return [REGISTER_RECORD]

    b.http = FakeHttp()
    geo = GeoIdentity(latitude=52.3, longitude=4.9, rd_x=1.0, rd_y=2.0,
                      bag_verblijfsobject_id="missing")
    facts = await b.for_location(geo, postal_code="1015AA", house_number="30")

    assert len(urls) == 2 and urls[1].endswith("/Adres")
    assert facts.energy_label == "C"


def test_a_house_letter_is_not_a_toevoeging():
    """The register treats "30-H" and "30-bis" as different kinds of thing."""
    from app.services.building import _split_addition

    assert _split_addition("H") == {"huisletter": "H"}
    assert _split_addition("bis") == {"huisnummertoevoeging": "bis"}
    assert _split_addition("2") == {"huisnummertoevoeging": "2"}
    assert _split_addition(None) == {}
