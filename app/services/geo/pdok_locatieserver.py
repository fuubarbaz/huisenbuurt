"""PDOK Locatieserver — address to coordinates + CBS area codes.

Free, keyless, and the canonical Dutch geocoder. Two calls:
  1. ``/free``   — search for the address, returns the best-matching document
                   with an ``id`` and a WKT centroid.
  2. ``/lookup`` — fetch the full document by id, which carries the
                   buurtcode / wijkcode / gemeentecode we join every open-data
                   layer on.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from app.core.config import settings
from app.core.http_client import HttpClient, TransientHTTPError
from app.models.property import GeoIdentity

log = logging.getLogger(__name__)

_POINT_RE = re.compile(r"POINT\(([-\d.]+)\s+([-\d.]+)\)")

# Fields worth asking for explicitly; the default field set omits the area codes.
_LOOKUP_FIELDS = ",".join([
    "id", "weergavenaam", "centroide_ll", "centroide_rd", "score",
    "postcode", "huisnummer", "huisletter", "huisnummertoevoeging", "huis_nlt",
    "straatnaam", "woonplaatsnaam",
    "adresseerbaarobject_id",
    "buurtcode", "buurtnaam", "wijkcode", "wijknaam",
    "gemeentecode", "gemeentenaam", "provincienaam", "nummeraanduiding_id",
])


class GeocodeError(Exception):
    """The address could not be resolved to a usable identity."""


class AddressMismatch(GeocodeError):
    """Locatieserver matched, but not the address that was asked for.

    The search is fuzzy and always returns its best guess: querying the
    nonexistent '9999ZZ 1' answers with a street in Maastricht at relevance
    3.04, against 11.29 for a true match. Scoring the wrong house silently is
    far worse than failing, so the returned postcode is verified.
    """


class PDOKLocatieserver:
    def __init__(self, http: HttpClient, base_url: str | None = None) -> None:
        self.http = http
        self.base_url = (base_url or settings.pdok_locatieserver_url).rstrip("/")

    async def resolve(
        self,
        *,
        postal_code: str,
        house_number: str,
        addition: Optional[str] = None,
    ) -> GeoIdentity:
        """Resolve a PC6 + house number to coordinates and CBS area codes."""
        query = f"{postal_code} {house_number}" + (f" {addition}" if addition else "")

        try:
            search = await self.http.get_json(
                f"{self.base_url}/free",
                params={"q": query, "fq": "type:adres", "rows": 1, "fl": _LOOKUP_FIELDS},
                headers={"Accept": "application/json"},
            )
        except TransientHTTPError as exc:
            raise GeocodeError(f"Locatieserver unreachable for {query!r}: {exc}") from exc

        docs = (search.get("response") or {}).get("docs") or []
        if not docs:
            raise GeocodeError(f"no address match for {query!r}")

        doc: dict[str, Any] = docs[0]

        # Verify the match is actually the address we asked for; see AddressMismatch.
        # A missing postcode is as damning as a wrong one: the '9999ZZ 1' match
        # carries none at all, having matched the street name "1 juli-weg".
        wanted = postal_code.replace(" ", "").upper()
        matched_pc = str(doc.get("postcode") or "").replace(" ", "").upper()
        if matched_pc != wanted:
            raise AddressMismatch(
                f"{query!r} resolved to {doc.get('weergavenaam')!r} "
                f"(postcode {matched_pc or 'absent'}, relevance {doc.get('score')})"
            )

        # A wrong house number inside the right postcode is a soft miss: the
        # location is still right to within a PC6, so it is recorded, not fatal.
        exact = _same_house_number(doc.get("huisnummer"), house_number)

        # The /free response often omits the area codes; /lookup always has them.
        if not doc.get("buurtcode") and doc.get("id"):
            doc = {**doc, **(await self._lookup(doc["id"]) or {})}

        lon, lat = self._parse_point(doc.get("centroide_ll"))
        if lat is None or lon is None:
            raise GeocodeError(f"match for {query!r} carried no usable centroid")

        rd_x, rd_y = self._parse_point(doc.get("centroide_rd"))

        return GeoIdentity(
            latitude=lat,
            longitude=lon,
            rd_x=rd_x,
            rd_y=rd_y,
            buurtcode=self._prefix(doc.get("buurtcode"), "BU"),
            buurtnaam=doc.get("buurtnaam"),
            wijkcode=self._prefix(doc.get("wijkcode"), "WK"),
            gemeentecode=self._prefix(doc.get("gemeentecode"), "GM"),
            gemeentenaam=doc.get("gemeentenaam"),
            provincie=doc.get("provincienaam"),
            bag_nummeraanduiding_id=doc.get("nummeraanduiding_id"),
            bag_verblijfsobject_id=doc.get("adresseerbaarobject_id"),
            matched_address=doc.get("weergavenaam"),
            match_score=doc.get("score"),
            exact_house_number=exact,
        )

    async def resolve_text(
        self,
        *,
        street: str,
        house_number: str,
        city: str,
        addition: Optional[str] = None,
    ) -> GeoIdentity:
        """Resolve an address given without a postcode.

        Used for a listing URL, where street/number/city are known but the
        postcode is not. The relevance score cannot police this: Locatieserver
        answers "huis-te-koop-mooi 1" with a real street in Leiden at 13.1,
        higher than a correct match for Singel 30. So the *returned* street and
        city are compared against what was asked for, and a disagreement is an
        error rather than a silently wrong house.
        """
        number = f"{house_number}{addition or ''}"
        query = f"{street} {number} {city}"

        try:
            search = await self.http.get_json(
                f"{self.base_url}/free",
                params={"q": query, "fq": "type:adres", "rows": 1, "fl": _LOOKUP_FIELDS},
                headers={"Accept": "application/json"},
            )
        except TransientHTTPError as exc:
            raise GeocodeError(f"Locatieserver unreachable for {query!r}: {exc}") from exc

        docs = (search.get("response") or {}).get("docs") or []
        if not docs:
            raise GeocodeError(f"no address match for {query!r}")

        doc: dict[str, Any] = docs[0]
        matched_street = str(doc.get("straatnaam") or "")
        matched_city = str(doc.get("woonplaatsnaam") or "")

        if not _loosely_equal(matched_street, street):
            raise AddressMismatch(
                f"{query!r} resolved to street {matched_street!r}, not {street!r} "
                f"({doc.get('weergavenaam')})"
            )
        if not _loosely_equal(matched_city, city):
            raise AddressMismatch(
                f"{query!r} resolved to {matched_city!r}, not {city!r} "
                f"({doc.get('weergavenaam')})"
            )

        postcode = str(doc.get("postcode") or "").replace(" ", "").upper()
        if not postcode:
            raise AddressMismatch(f"{query!r} matched an address with no postcode")

        return await self.resolve(postal_code=postcode, house_number=house_number,
                                  addition=addition)

    async def search(self, query: str) -> Optional[GeoIdentity]:
        """Loose geocode of free text — a station, a city, a street, an address.

        The search is fuzzy and always answers, so the match is checked against
        the words that were asked for: "Utrecht Centraal" otherwise resolves to
        a bus station in *Breda*, and "Schiphol Airport" to *Maastricht*. Both
        share one word with the query and nothing else. A confidently wrong
        commute time is worse than none, so a match that drops a significant
        word is rejected and the caller is told to be more specific.
        """
        query = query.strip()
        if not query:
            return None

        # Coordinates, however they arrived, are already the answer — a paste
        # from Google Maps is more precise than anything a text search returns.
        point = parse_coordinates(query)
        if point is not None:
            lat, lon = point
            return GeoIdentity(latitude=lat, longitude=lon,
                               matched_address=f"{lat:.5f}, {lon:.5f}")

        query = strip_place_url(query)
        try:
            payload = await self.http.get_json(
                f"{self.base_url}/free",
                params={"q": query.strip(), "rows": 1, "fl": _LOOKUP_FIELDS,
                        "fq": "type:(adres OR weg OR woonplaats OR gemeente OR postcode)"},
                headers={"Accept": "application/json"},
            )
        except TransientHTTPError as exc:
            raise GeocodeError(f"Locatieserver unreachable for {query!r}: {exc}") from exc

        docs = (payload.get("response") or {}).get("docs") or []
        if not docs:
            return None
        doc = docs[0]
        if not _covers_query(query, str(doc.get("weergavenaam") or "")):
            raise AddressMismatch(
                f"{query!r} best matches {doc.get('weergavenaam')!r}, which does not "
                "contain what was asked for — try a postcode or a full address"
            )
        lon, lat = self._parse_point(doc.get("centroide_ll"))
        if lat is None or lon is None:
            return None
        rd_x, rd_y = self._parse_point(doc.get("centroide_rd"))
        return GeoIdentity(
            latitude=lat, longitude=lon, rd_x=rd_x, rd_y=rd_y,
            matched_address=doc.get("weergavenaam"), match_score=doc.get("score"),
            gemeentenaam=doc.get("gemeentenaam"),
        )

    async def suggest(self, query: str, *, limit: int = 8) -> list[dict[str, str]]:
        """Live suggestions as the address is typed, the way Google Maps does it.

        PDOK's ``/suggest`` endpoint, not ``/free``: it is tuned for partial,
        still-being-typed input ("Cruquiuskade 28") and answers fast with just
        an id and a display name. Full detail comes from a follow-up
        ``address_by_id`` once the user actually picks one — verified live:
        ``/suggest`` for a house number with a letter returns a SEPARATE
        suggestion per letter ("289", "289A", "289B", "289C"), so picking one
        is already unambiguous; there is no fuzzy-match risk here the way
        there is in ``resolve()``, because nothing was guessed.

        Degrades to an empty list on any upstream failure — a stalled
        autocomplete dropdown is a UI inconvenience, not a reason to surface
        an error to someone mid-keystroke.
        """
        query = query.strip()
        if len(query) < 3:
            return []
        try:
            payload = await self.http.get_json(
                f"{self.base_url}/suggest",
                params={"q": query, "rows": limit, "fq": "type:adres"},
                headers={"Accept": "application/json"},
            )
        except TransientHTTPError as exc:
            log.warning("Locatieserver suggest failed for %r: %s", query, exc)
            return []
        docs = (payload.get("response") or {}).get("docs") or []
        return [{"id": d["id"], "label": d["weergavenaam"]}
                for d in docs if d.get("id") and d.get("weergavenaam")]

    async def address_by_id(self, doc_id: str) -> dict[str, Any]:
        """The postcode/number/addition for a suggestion the user picked.

        Deliberately returns raw form fields, not a :class:`GeoIdentity` — the
        actual geocode still happens through ``resolve()`` when the score
        request comes in, so there is exactly one code path that ever builds a
        GeoIdentity and one place a wrong-address bug could live. This method
        exists only to save someone typing what they already picked from a
        dropdown.
        """
        doc = await self._lookup(doc_id)
        if not doc:
            raise GeocodeError(f"no address found for suggestion id {doc_id!r}")

        postcode = str(doc.get("postcode") or "").replace(" ", "").upper()
        number = str(doc.get("huisnummer") or "")
        # huis_nlt already combines number + letter + toevoeging exactly the
        # way resolve()'s own query building expects an addition appended
        # ("289A", "289-2"); stripping the bare number back off it is more
        # robust than reassembling huisletter + huisnummertoevoeging by hand,
        # since PDOK's own separator conventions between the two are not
        # documented and are easy to get subtly wrong.
        nlt = str(doc.get("huis_nlt") or "")
        addition = nlt[len(number):] or None if nlt.startswith(number) else None

        if not postcode or not number:
            raise GeocodeError(f"suggestion {doc_id!r} carried no usable postcode/number")

        return {
            "postal_code": postcode,
            "house_number": number,
            "house_number_addition": addition,
            "address": doc.get("weergavenaam"),
        }

    async def _lookup(self, doc_id: str) -> dict[str, Any] | None:
        try:
            data = await self.http.get_json(
                f"{self.base_url}/lookup",
                params={"id": doc_id, "fl": _LOOKUP_FIELDS},
                headers={"Accept": "application/json"},
            )
        except TransientHTTPError as exc:
            log.warning("Locatieserver lookup failed for %s: %s", doc_id, exc)
            return None
        docs = (data.get("response") or {}).get("docs") or []
        return docs[0] if docs else None

    @staticmethod
    def _prefix(code: str | None, prefix: str) -> str | None:
        """Locatieserver returns gemeentecode bare ('0363') but buurt/wijk codes
        already prefixed. CBS OData keys on the prefixed form throughout, so
        normalise here rather than in every provider."""
        if not code:
            return None
        code = code.strip()
        return code if code.upper().startswith(prefix) else f"{prefix}{code}"

    @staticmethod
    def _parse_point(wkt: str | None) -> tuple[float | None, float | None]:
        """Parse 'POINT(x y)'. Returns (x, y) — for centroide_ll that is (lon, lat)."""
        if not wkt:
            return (None, None)
        match = _POINT_RE.search(wkt)
        if not match:
            return (None, None)
        return (float(match.group(1)), float(match.group(2)))


def _same_house_number(matched: Any, requested: str) -> bool:
    """Whether Locatieserver returned the house number that was asked for."""
    digits = "".join(ch for ch in str(requested) if ch.isdigit())
    if not digits or matched is None:
        return False
    try:
        return int(matched) == int(digits)
    except (TypeError, ValueError):
        return False


def _loosely_equal(a: str, b: str) -> bool:
    """Compare place names ignoring case, punctuation and spacing.

    "'s-Hertogenbosch" from Locatieserver has to match "S Hertogenbosch" as
    recovered from a URL slug, and "Bos en Vaartlaan" must match itself.
    """
    normalise = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())  # noqa: E731
    left, right = normalise(a), normalise(b)
    if not left or not right:
        return False
    return left == right or left.endswith(right) or right.endswith(left)


#: Words too generic to distinguish one place from another. Country names are
#: here because Google Maps appends one when you copy an address, and PDOK —
#: being a Dutch register — never echoes it back.
_GENERIC_PLACE_WORDS = {
    "station", "centraal", "airport", "luchthaven", "centrum", "the", "de", "het",
    "van", "en", "nl", "nederland", "netherlands", "holland", "nld",
    "gemeente", "straat", "weg", "laan", "plein",
}

#: Google Maps writes a point two different ways, so these stay two patterns
#: rather than one clever one: the viewport marker (…/@52.3791,4.9003,17z) and
#: the data layer further along the URL (…!3d52.3791!4d4.9003).
_MAPS_AT_RE = re.compile(r"@(-?\d{1,2}\.\d{3,}),(-?\d{1,3}\.\d{3,})")
_MAPS_DATA_RE = re.compile(r"!3d(-?\d{1,2}\.\d{3,})!4d(-?\d{1,3}\.\d{3,})")

#: What "copy coordinates" puts on the clipboard.
_BARE_COORDS_RE = re.compile(r"^\s*(-?\d{1,2}\.\d{3,})\s*,\s*(-?\d{1,3}\.\d{3,})\s*$")

#: Roughly the Netherlands, so a stray pair of numbers is not read as an address.
_NL_BOUNDS = (50.7, 53.6, 3.2, 7.3)   # min lat, max lat, min lon, max lon


def _covers_query(query: str, matched: str) -> bool:
    """Whether the match keeps the distinctive words that were asked for.

    Generic words are ignored: "Centraal" alone must not make a bus station in
    Breda look like a match for "Utrecht Centraal".
    """
    normalise = lambda s: re.sub(r"[^a-z0-9 ]", " ", s.lower())  # noqa: E731
    matched_words = set(normalise(matched).split())
    asked = [w for w in normalise(query).split()
             if len(w) > 2 and w not in _GENERIC_PLACE_WORDS]
    if not asked:
        return True          # nothing distinctive was asked for
    return all(
        any(word == m or m.startswith(word) or word.startswith(m) for m in matched_words)
        for word in asked
    )


def parse_coordinates(text: str) -> Optional[tuple[float, float]]:
    """Latitude and longitude out of a Google Maps URL or a bare pair.

    Returns None unless the point is inside the Netherlands: every data source
    behind this engine is Dutch, so a coordinate elsewhere is a mistake worth
    surfacing rather than a location worth scoring.
    """
    match = (_BARE_COORDS_RE.match(text)
             or _MAPS_DATA_RE.search(text)
             or _MAPS_AT_RE.search(text))
    if not match:
        return None
    try:
        lat, lon = float(match.group(1)), float(match.group(2))
    except (TypeError, ValueError):
        return None
    min_lat, max_lat, min_lon, max_lon = _NL_BOUNDS
    if not (min_lat <= lat <= max_lat and min_lon <= lon <= max_lon):
        return None
    return lat, lon


def strip_place_url(text: str) -> str:
    """Recover a place name from a Google Maps URL that carries no coordinates.

    ``/maps/place/Amsterdam+Centraal/`` becomes "Amsterdam Centraal". A URL
    left intact would be searched verbatim and match nothing.
    """
    match = re.search(r"/maps/place/([^/@?]+)", text)
    if not match:
        return text
    from urllib.parse import unquote
    return unquote(match.group(1)).replace("+", " ").strip()
