"""Which listings the watch loop bothers with.

Without this, the loop watches the entire country: a real run alerted on
houses in Almelo and Leeuwarden for someone whose interest was Amsterdam. The
fix has to sit before enrichment, not after — a listing outside the region
should never cost a geocode or the twelve calls behind it, the same reasoning
that already applies to the price and floor-area filters in the orchestrator.

What makes this cheap: Huispedia's URL shape is ``/{city}/{pc6}/{street}/...``,
so the scraper already knows the city and postcode from discovery, before any
detail page is fetched. Filtering here costs nothing beyond a string compare.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from app.models.property import PropertyListing


@dataclass(frozen=True)
class RegionFilter:
    """A listing passes if it matches ANY configured city or postcode prefix.

    Empty means unfiltered — the historical behaviour, and what an unset
    ``WOONAGENT_REGIONS`` must continue to mean, so existing setups are not
    narrowed to nothing by this feature landing.
    """

    cities: frozenset[str]
    pc4_prefixes: frozenset[str]

    @property
    def is_empty(self) -> bool:
        return not self.cities and not self.pc4_prefixes

    def matches(self, listing: PropertyListing) -> bool:
        if self.is_empty:
            return True
        if listing.city and listing.city.strip().casefold() in self.cities:
            return True
        pc4 = listing.postal_code[:4] if listing.postal_code else ""
        return any(pc4.startswith(prefix) for prefix in self.pc4_prefixes)

    def __str__(self) -> str:
        if self.is_empty:
            return "none (unfiltered)"
        parts = sorted(self.cities) + sorted(self.pc4_prefixes)
        return ", ".join(parts)


class InvalidRegion(ValueError):
    """A region token that is neither a city name nor a postcode prefix."""


#: A bare PC4, or a PC4 immediately followed by letters (a PC6 or a partial
#: one) — "1018", "1018AM" and "1018A" are all accepted as the PC4 prefix.
_PC4_LIKE = re.compile(r"^\d{4}[A-Za-z]{0,2}$")


def parse_regions(raw: Optional[str]) -> RegionFilter:
    """Turn what a user typed into a filter.

    Accepts a comma-separated mix of city names ("Amsterdam") and postcode
    prefixes ("1018", or a full PC6 — only the first four digits are used,
    since that is the granularity Huispedia's URL gives before enrichment).
    Blank input, ``None``, and pure whitespace all mean "no filter" rather
    than an error, so clearing the field in the UI behaves as expected.
    """
    if not raw or not raw.strip():
        return RegionFilter(frozenset(), frozenset())

    cities: set[str] = set()
    prefixes: set[str] = set()
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        if _PC4_LIKE.match(token):
            prefixes.add(token[:4])
        elif token.replace(" ", "").replace("-", "").replace("'", "").isalpha():
            # Dutch place names commonly carry an apostrophe: 's-Hertogenbosch,
            # 's-Gravenhage. A bare .isalpha() would reject both as garbage.
            cities.add(token.casefold())
        else:
            raise InvalidRegion(
                f"{token!r} is not a city name or a postcode — "
                "try 'Amsterdam' or '1018'"
            )
    return RegionFilter(frozenset(cities), frozenset(prefixes))
