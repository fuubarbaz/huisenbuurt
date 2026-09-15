"""Huispedia monitor.

Huispedia publishes a robots.txt that permits generic crawlers on listing
content (it blocks only tracking and contact endpoints), advertises a sitemap
index, and marks up every property page with schema.org JSON-LD. This scraper
uses those three published interfaces rather than parsing search-result markup:

* **Discovery** comes from ``properties-listed-*.xml.gz`` in the sitemap index,
  where each entry carries a ``lastmod`` date. Filtering on that date is how
  "newly listed" is determined — no search pagination, no guessing.
* **Addressing** comes from the URL itself, shaped
  ``/{city}/{pc6}/{street}/{house-number}``, so the postcode and house number
  are known before any detail page is fetched.
* **Facts** come from the page's JSON-LD (`SingleFamilyResidence` and
  `Product`), which carries address, coordinates, floor size, room count and
  price as structured data.

Only the construction year needs markup parsing — it lives in a feature list
rather than the JSON-LD — and it matters enough to the foundation-risk layer to
be worth the fragility. Everything else degrades gracefully if the markup
changes, because it comes from the structured block.
"""
from __future__ import annotations

import gzip
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, AsyncIterator, Iterable, Optional

from app.core.config import settings
from app.core.rate_limit import human_delay
from app.core.robots import RobotsDisallowed
from app.models.property import ListingSource, PropertyListing
from app.scrapers.base import BaseScraper

log = logging.getLogger(__name__)

SITEMAP_INDEX = "https://huispedia.nl/sitemap.xml"
LISTED_SITEMAP_MARKER = "properties-listed"

#: /{city}/{pc6}/{street}/{house-number}
URL_PATH_RE = re.compile(
    r"^https?://[^/]+/(?P<city>[^/]+)/(?P<pc6>\d{4}[a-z]{2})/(?P<street>[^/]+)/(?P<number>[^/?#]+)/?$",
    re.IGNORECASE,
)
SITEMAP_ENTRY_RE = re.compile(
    r"<url>\s*<loc>(?P<loc>[^<]+)</loc>\s*<lastmod>(?P<lastmod>[^<]+)</lastmod>", re.S
)
LOC_RE = re.compile(r"<loc>([^<]+)</loc>")
JSONLD_RE = re.compile(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', re.S)
FEATURE_RE = re.compile(
    r'<span class="name">\s*(?P<name>.*?)\s*</span>\s*<span class="value">\s*(?P<value>.*?)\s*</span>',
    re.S,
)
TAG_RE = re.compile(r"<[^>]+>")

#: "289-b" / "43-15" / "3" -> the numeric part and whatever trails it. The URL
#: joins number and addition with a hyphen; the model keeps them apart because
#: PDOK and the BAG treat them as separate fields.
HOUSE_NUMBER_RE = re.compile(r"^(?P<number>\d+)[-\s]*(?P<addition>.*)$")

#: How many of the newest listed-sitemaps to scan. New entries land in the
#: highest-numbered files, so a handful covers a day's listings without
#: pulling all 46 every cycle.
DEFAULT_SITEMAPS_TO_SCAN = 3


class HuispediaScraper(BaseScraper):
    source = ListingSource.HUISPEDIA

    async def discover(
        self,
        *,
        since: Optional[date] = None,
        sitemaps_to_scan: int = DEFAULT_SITEMAPS_TO_SCAN,
        max_listings: int = 100,
        **_filters: Any,
    ) -> AsyncIterator[PropertyListing]:
        """Yield properties whose sitemap entry changed on or after ``since``.

        Defaults to the last two days, which comfortably covers a 5-15 minute
        poll cycle while tolerating a missed run.
        """
        cutoff = since or (datetime.now(timezone.utc).date() - timedelta(days=1))

        try:
            await self.robots.require(SITEMAP_INDEX)
        except RobotsDisallowed as exc:
            log.error("refusing to scrape huispedia: %s", exc)
            return

        urls = await self._recent_urls(cutoff, sitemaps_to_scan)
        log.info("huispedia: %d listing(s) modified since %s", len(urls), cutoff)

        yielded = 0
        for url in urls:
            if yielded >= max_listings:
                break
            listing = await self._fetch_listing(url)
            if listing is not None:
                yielded += 1
                yield listing
            # Politeness pause between detail pages, on top of the host budget.
            await human_delay(
                settings.request_delay_min_seconds, settings.request_delay_max_seconds
            )

    # -- discovery ---------------------------------------------------------

    async def _recent_urls(self, cutoff: date, sitemaps_to_scan: int) -> list[str]:
        index = await self.http.get_text(SITEMAP_INDEX)
        listed = [u for u in LOC_RE.findall(index) if LISTED_SITEMAP_MARKER in u]
        if not listed:
            log.warning("no %s sitemaps in the index", LISTED_SITEMAP_MARKER)
            return []

        recent: list[str] = []
        for sitemap_url in sorted(listed)[-sitemaps_to_scan:]:
            try:
                recent.extend(await self._urls_from(sitemap_url, cutoff))
            except Exception as exc:  # noqa: BLE001 — one bad sitemap is not fatal
                log.warning("sitemap %s failed: %s", sitemap_url, exc)
        # Newest first, and de-duplicated across sitemaps.
        return list(dict.fromkeys(reversed(recent)))

    async def _urls_from(self, sitemap_url: str, cutoff: date) -> list[str]:
        response = await self.http.request("GET", sitemap_url)
        body = response.content
        if sitemap_url.endswith(".gz"):
            body = gzip.decompress(body)
        xml = body.decode("utf-8", errors="replace")

        found = []
        for match in SITEMAP_ENTRY_RE.finditer(xml):
            modified = _parse_date(match.group("lastmod"))
            if modified and modified >= cutoff:
                found.append(match.group("loc"))
        return found

    # -- one listing -------------------------------------------------------

    async def _fetch_listing(self, url: str) -> Optional[PropertyListing]:
        parts = URL_PATH_RE.match(url)
        if not parts:
            log.debug("unrecognised listing url shape: %s", url)
            return None

        try:
            await self.robots.require(url)
        except RobotsDisallowed as exc:
            log.warning("skipping %s: %s", url, exc)
            return None

        try:
            html = await self.http.get_text(url)
        except Exception as exc:  # noqa: BLE001 — a dead listing is not fatal
            log.warning("could not fetch %s: %s", url, exc)
            return None

        return self.parse(url, html, parts.groupdict())

    @classmethod
    def parse(cls, url: str, html: str, parts: dict[str, str]) -> Optional[PropertyListing]:
        """Build a listing from the page. Pure, so it is testable offline."""
        nodes = _jsonld_nodes(html)
        residence = _node_of_type(nodes, ("SingleFamilyResidence", "Residence", "Apartment", "House"))
        product = _node_of_type(nodes, ("Product",))
        features = _features(html)

        address = (residence or {}).get("address") or {}
        postcode = (address.get("postalCode") or parts["pc6"]).replace(" ", "").upper()
        number, addition = _split_house_number(parts["number"])

        price = _price(product) or _euros(features.get("Vraagprijs"))
        area = _int(_first_number(features.get("Woonoppervlakte"))) or _int((residence or {}).get("floorSize"))
        rooms = _int(_first_number(features.get("Aantal kamers"))) or _int((residence or {}).get("numberOfRooms"))

        try:
            return PropertyListing(
                property_id=cls.property_id_for(url),
                source=cls.source,
                url=url,
                address=(residence or {}).get("name")
                        or f"{_titlecase(parts['street'])} {parts['number']}, {_titlecase(parts['city'])}",
                postal_code=postcode,
                house_number=number,
                house_number_addition=addition,
                city=address.get("addressLocality") or _titlecase(parts["city"]),
                price_eur=price,
                living_area_m2=area,
                rooms=rooms,
                construction_year=_int(_first_number(features.get("Bouwjaar"))),
                listed_at=_listed_at(features.get("Aangeboden sinds")),
            )
        except Exception as exc:  # noqa: BLE001 — a malformed page is not fatal
            log.warning("could not build a listing from %s: %s", url, exc)
            return None

    @staticmethod
    def property_id_for(url: str) -> str:
        """Stable id derived from the address path, not from a session token."""
        path = re.sub(r"^https?://[^/]+/", "", url).strip("/").lower()
        return f"huispedia:{path}"


# -- parsing helpers ---------------------------------------------------------


def _jsonld_nodes(html: str) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for block in JSONLD_RE.findall(html):
        try:
            parsed = json.loads(block.strip())
        except json.JSONDecodeError:
            continue
        nodes.extend(parsed if isinstance(parsed, list) else [parsed])
    return [n for n in nodes if isinstance(n, dict)]


def _node_of_type(nodes: Iterable[dict[str, Any]], wanted: tuple[str, ...]) -> Optional[dict[str, Any]]:
    for node in nodes:
        declared = node.get("@type")
        declared = declared if isinstance(declared, list) else [declared]
        if any(t in wanted for t in declared if t):
            return node
    return None


def _features(html: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for match in FEATURE_RE.finditer(html):
        name = TAG_RE.sub("", match.group("name")).strip()
        value = TAG_RE.sub("", match.group("value")).strip()
        if name and name not in out:
            out[name] = value
    return out


def _price(product: Optional[dict[str, Any]]) -> Optional[int]:
    offer = (product or {}).get("offers") or {}
    return _int(offer.get("price"))


def _euros(text: Optional[str]) -> Optional[int]:
    """'€ 625.000' -> 625000. Dutch thousands separators are dots."""
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", text.split(",")[0])
    return int(digits) if digits else None


def _first_number(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    match = re.search(r"\d+", text.replace(".", ""))
    return match.group(0) if match else None


def _int(value: Any) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _parse_date(raw: str) -> Optional[date]:
    try:
        return datetime.fromisoformat(raw.strip().replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(raw.strip()[:10])
        except ValueError:
            return None


def _listed_at(raw: Optional[str]) -> Optional[datetime]:
    """'7 uur' / '3 dagen' -> an absolute timestamp.

    Huispedia states how long a listing has been up, not when it went up, so
    the relative figure is anchored to now. Stored absolute because a relative
    string means something different every time it is read.
    """
    if not raw:
        return None
    match = re.search(r"(\d+)\s*(uur|dag|dagen|week|weken|maand|maanden|minuut|minuten)", raw, re.I)
    if not match:
        return None
    amount, unit = int(match.group(1)), match.group(2).lower()
    delta = {
        "minuut": timedelta(minutes=1), "minuten": timedelta(minutes=1),
        "uur": timedelta(hours=1),
        "dag": timedelta(days=1), "dagen": timedelta(days=1),
        "week": timedelta(weeks=1), "weken": timedelta(weeks=1),
        "maand": timedelta(days=30), "maanden": timedelta(days=30),
    }[unit]
    return datetime.now(timezone.utc) - amount * delta


def _split_house_number(raw: str) -> tuple[str, Optional[str]]:
    match = HOUSE_NUMBER_RE.match(raw.strip())
    if not match:
        return raw.strip(), None
    addition = match.group("addition").strip() or None
    return match.group("number"), addition


def _titlecase(slug: str) -> str:
    return " ".join(part.capitalize() for part in slug.replace("-", " ").split())
