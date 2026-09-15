"""Building facts from the national registries, for details a listing URL omits.

A listing URL carries the address and nothing else. Construction year, floor
area and energy label live in the page body — but they also live in official
Dutch registries, which are authoritative, free of any terms problem, and work
for every address rather than only for one portal's listings.

* **BAG** (Basisregistratie Adressen en Gebouwen) — the national building
  registry, via PDOK's WFS. Gives ``bouwjaar`` and floor area with no API key.
  Its ``oppervlakte`` is the *gebruiksoppervlakte* under NEN 2580 — usually the
  same number a listing advertises as woonoppervlakte, though not by
  definition: the two treat some spaces differently, so expect agreement within
  a few m² rather than exactly.
* **EP-Online** — RVO's energy-label register. Needs a free API key; without
  one the label is simply absent. Two documented endpoints are used, and the
  precise one is preferred: ``/PandEnergielabel/AdresseerbaarObject/{id}``
  takes the BAG dwelling id PDOK already hands us, so there is no ambiguity
  about which flat in a block is meant. ``/PandEnergielabel/Adres`` is the
  fallback, and splits ``huisletter`` from ``huisnummertoevoeging`` because the
  register treats "30-H" and "30-bis" as different kinds of thing.

  The register returns more than a letter. ``Gebouwtype`` is the woningtype the
  renovation cost tables are keyed on, so a label also tells the estimator
  whether this is a terraced house or a detached one.

Why this is not one of the six enrichment layers
------------------------------------------------
These are facts about the *property*, not about its surroundings, and one of
them is an *input* to the scoring: the soil provider decides pile-rot exposure
from the construction year. So this runs before enrichment and fills gaps in
the listing, rather than producing a score of its own.

A note on which year to believe. For a building containing several dwellings
the BAG records a year on the *pand* (the structure) and another on each
*verblijfsobject* (the unit inside it) — Singel 30 in Amsterdam reads 1730 and
1702 respectively. The foundation belongs to the structure, so the pand year is
the one used for risk, and the unit year is kept alongside it for reference.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from app.core.config import settings
from app.core.http_client import HttpClient, TransientHTTPError
from app.models.building import BuildingFacts
from app.models.property import GeoIdentity, PropertyListing
from app.services.enrichment.providers.wfs import WFSClient

log = logging.getLogger(__name__)

PAND = "bag:pand"
VERBLIJFSOBJECT = "bag:verblijfsobject"

#: A tight box: BAG polygons are building footprints, and a wide one would
#: catch the neighbours.
FOOTPRINT_EXTENT_M = 8.0

#: Energy labels, best to worst, as EP-Online reports them.
ENERGY_LABELS = ["A+++++", "A++++", "A+++", "A++", "A+", "A", "B", "C", "D", "E", "F", "G"]

#: EP-Online's Gebouwtype, mapped to the house types the renovation cost
#: tables use. Anything not listed (an apartment, say) stays None.
GEBOUWTYPE_TO_HOUSE_TYPE = {
    "tussenwoning": "terraced",
    "rijwoning tussen": "terraced",
    "hoekwoning": "end_terrace",
    "rijwoning hoek": "end_terrace",
    "2 onder 1 kap": "semi_detached",
    "twee-onder-een-kap": "semi_detached",
    "vrijstaande woning": "detached",
    "vrijstaand": "detached",
}


class BuildingLookup:
    """Reads BAG, and EP-Online when a key is configured."""

    def __init__(self, http: HttpClient, bag_url: str | None = None) -> None:
        self.http = http
        self.bag = WFSClient(http, bag_url or settings.bag_wfs)

    async def for_location(
        self, geo: GeoIdentity, *, postal_code: str | None = None,
        house_number: str | None = None, addition: str | None = None,
    ) -> BuildingFacts:
        """Look up everything available for one address."""
        pand = await self._first(PAND, geo)

        # Prefer the dwelling PDOK identified by id. A spatial query cannot
        # tell one flat from another inside a shared footprint; the id can.
        unit = None
        if geo.bag_verblijfsobject_id:
            unit = await self._by_id(VERBLIJFSOBJECT, geo.bag_verblijfsobject_id)
        exact_unit = unit is not None
        if unit is None:
            unit = await self._first(VERBLIJFSOBJECT, geo)

        units = _int((pand or {}).get("aantal_verblijfsobjecten"))
        # Without the id, a unit area is only trustworthy when the building
        # holds exactly one dwelling — otherwise the box query returned an
        # arbitrary neighbour. A failed pand lookup loses the count too, which
        # is the same problem with less evidence.
        area, exact = None, True
        if unit and exact_unit:
            area = _int(unit.get("oppervlakte"))
        elif unit and units == 1:
            area = _int(unit.get("oppervlakte"))
        elif unit and units is None:
            area, exact = _int(unit.get("oppervlakte")), False
        elif pand:
            low, high = _int(pand.get("oppervlakte_min")), _int(pand.get("oppervlakte_max"))
            if low is not None and high is not None and low == high:
                area = low
            elif high is not None:
                area, exact = high, False

        label = await self._energy_label(
            postal_code, house_number, addition, bag_id=geo.bag_verblijfsobject_id)

        return BuildingFacts(
            construction_year=_year(_int((pand or {}).get("bouwjaar"))),
            unit_construction_year=_year(_int((unit or {}).get("bouwjaar"))),
            floor_area_m2=area,
            floor_area_is_exact=exact,
            units_in_building=units,
            use=(pand or {}).get("gebruiksdoel"),
            status=(pand or {}).get("status"),
            energy_label=label.get("energy_label"),
            energy_label_valid_until=label.get("energy_label_valid_until"),
            energy_label_registered=label.get("energy_label_registered"),
            house_type=label.get("house_type"),
        )

    async def _by_id(self, type_name: str, identifier: str) -> Optional[dict[str, Any]]:
        try:
            return await self.bag.feature_by_id(
                type_name, field="identificatie", value=identifier)
        except Exception as exc:  # noqa: BLE001 — fall back to the spatial query
            log.warning("BAG %s lookup by id failed: %s", type_name, exc)
            return None

    async def _first(self, type_name: str, geo: GeoIdentity) -> Optional[dict[str, Any]]:
        try:
            rows = await self.bag.features_at(
                type_name, rd_x=geo.rd_x, rd_y=geo.rd_y,
                lat=geo.latitude, lon=geo.longitude,
                half_extent_m=FOOTPRINT_EXTENT_M, count=1,
            )
        except Exception as exc:  # noqa: BLE001 — a missing fact is not fatal
            log.warning("BAG %s lookup failed: %s", type_name, exc)
            return None
        return rows[0] if rows else None

    async def _energy_label(
        self, postal_code: Optional[str], house_number: Optional[str],
        addition: Optional[str] = None, bag_id: Optional[str] = None,
    ) -> dict[str, Optional[str]]:
        """The registered label, if a key is configured. Absent otherwise.

        Prefers the BAG dwelling id: it names one dwelling exactly, where a
        postcode and number can match several flats in a block.
        """
        empty: dict[str, Optional[str]] = {}
        if not settings.eponline_api_key:
            return empty

        record = None
        if bag_id:
            record = await self._eponline(
                f"{settings.eponline_url.rstrip('/')}/AdresseerbaarObject/{bag_id}")
        if record is None and postal_code and house_number:
            params: dict[str, str] = {"postcode": postal_code, "huisnummer": house_number}
            params.update(_split_addition(addition))
            record = await self._eponline(
                f"{settings.eponline_url.rstrip('/')}/Adres", params=params)
        if record is None:
            return empty

        gebouwtype = str(record.get("Gebouwtype") or "").strip().lower()
        return {
            "energy_label": record.get("Energieklasse"),
            "energy_label_valid_until": record.get("Geldig_tot"),
            "energy_label_registered": record.get("Registratiedatum"),
            "house_type": GEBOUWTYPE_TO_HOUSE_TYPE.get(gebouwtype),
        }

    async def _eponline(self, url: str, params: dict | None = None) -> Optional[dict]:
        """One EP-Online call. A 404 means "no registration", which is normal."""
        try:
            payload = await self.http.get_json(
                url, params=params,
                headers={"Authorization": settings.eponline_api_key,
                         "Accept": "application/json"},
            )
        except TransientHTTPError as exc:
            log.warning("EP-Online lookup failed: %s", exc)
            return None
        except Exception as exc:  # noqa: BLE001 — a 404 is an ordinary outcome
            log.debug("EP-Online returned no registration: %s", exc)
            return None
        records = payload if isinstance(payload, list) else [payload]
        return records[0] if records and isinstance(records[0], dict) else None


def _split_addition(addition: Optional[str]) -> dict[str, str]:
    """The register separates a house *letter* from a *toevoeging*.

    "30-H" is a huisletter; "30-bis" and "30-2" are toevoegingen. Sending one
    as the other returns no registration.
    """
    value = (addition or "").strip()
    if not value:
        return {}
    if len(value) == 1 and value.isalpha():
        return {"huisletter": value.upper()}
    return {"huisnummertoevoeging": value}


def fill_listing(listing: PropertyListing, facts: BuildingFacts) -> PropertyListing:
    """Fill only the gaps: what the listing already states always wins.

    The seller knows things the registry does not — a converted attic, a
    renovation — and a scraped figure is what the buyer will actually see
    advertised. The registry is here to fill blanks, not to argue.
    """
    updates: dict[str, Any] = {}
    if listing.construction_year is None and facts.construction_year is not None:
        updates["construction_year"] = facts.construction_year
    if listing.living_area_m2 is None and facts.floor_area_m2 is not None and facts.floor_area_is_exact:
        updates["living_area_m2"] = facts.floor_area_m2
    return listing.model_copy(update=updates) if updates else listing


def _int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _year(value: Optional[int]) -> Optional[int]:
    """BAG uses 1005 as a placeholder for "unknown, but old"."""
    if value is None or value < 1100 or value > 2100:
        return None
    return value
