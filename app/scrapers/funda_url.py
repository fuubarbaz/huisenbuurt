"""Read an address out of a Funda listing URL, without fetching Funda.

Funda blocks automated access (see :mod:`app.scrapers.funda`), but its listing
URLs are handed to you by the person browsing, and they already carry the
address:

    https://www.funda.nl/detail/koop/amsterdam/huis-singel-30/43829102/
                                    ^city      ^type ^street ^nr  ^listing id

That is everything the engine needs: PDOK resolves street + number + city to a
postcode, coordinates and CBS area codes, and the six enrichment layers key off
those. Nothing is requested from Funda at any point — the URL is treated as a
string the user pasted, which is exactly what it is.

What this cannot recover is what only the page body holds: asking price, floor
area and construction year. Pass those with ``--price``, ``--area`` and
``--year`` from the listing you are already looking at. Construction year is
the one worth typing — it drives the foundation-risk check.

A caveat on the patterns below: they were written from Funda's published URL
shapes and cannot be verified against the live site, because we do not fetch
it. If a URL fails to parse, the address can always be given directly instead.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import unquote, urlparse

#: Dwelling-type prefixes Funda puts at the front of the address slug.
DWELLING_TYPES = (
    "huis", "woonhuis", "appartement", "studio", "kamer", "villa",
    "bouwgrond", "parkeergelegenheid", "berging", "ligplaats", "object",
)

#: Segments that are route structure, never the city.
ROUTE_WORDS = {"detail", "koop", "huur", "koopwoning", "huurwoning", "nl", "en"}

#: A Funda listing id: a long run of digits, distinct from a house number.
LISTING_ID_RE = re.compile(r"^\d{6,}$")

#: Trailing house number, optionally with an addition: 30, 30-a, 12-bis, 289-b.
HOUSE_NUMBER_RE = re.compile(r"^(?P<number>\d+)(?:[-\s]?(?P<addition>[a-z0-9]{1,6}))?$", re.I)


@dataclass(frozen=True)
class FundaAddress:
    street: str
    house_number: str
    city: str
    addition: Optional[str] = None
    listing_id: Optional[str] = None

    @property
    def query(self) -> str:
        """Free-text form for the geocoder."""
        number = f"{self.house_number}{self.addition or ''}"
        return f"{self.street} {number} {self.city}"


class FundaUrlUnparseable(ValueError):
    """The URL did not look like a Funda listing address."""


def parse_funda_url(url: str) -> FundaAddress:
    """Pull street, house number and city out of a Funda listing URL.

    Raises :class:`FundaUrlUnparseable` rather than guessing — a wrong address
    scored as the right one is worse than an error, and the caller can always
    fall back to typing the address.
    """
    parsed = urlparse(url if "//" in url else f"https://{url}")
    if "funda.nl" not in (parsed.netloc or "").lower():
        raise FundaUrlUnparseable(f"not a funda.nl URL: {url!r}")

    segments = [unquote(s).lower() for s in parsed.path.split("/") if s]
    if not segments:
        raise FundaUrlUnparseable("URL has no path")

    slug_index = _address_slug_index(segments)
    if slug_index is None:
        if _is_bare_share_link(segments):
            # The exact wording "shared from Funda's app" is matched by the UI
            # (FUNDA_SHARE_LINK_MARKER in app/static/index.html) to show a
            # guided fix instead of a plain error banner. Keep both in sync.
            raise FundaUrlUnparseable(
                "this looks like a link shared from Funda's app — those encode only "
                "the listing id, not the address, so there is nothing here to read. "
                "Open the listing in a browser and paste that URL instead (it looks "
                "like /detail/koop/<city>/huis-<street>-<number>/<id>/), or enter the "
                "address directly."
            )
        raise FundaUrlUnparseable(
            "no address slug found; expected something like "
            "/detail/koop/<city>/huis-<street>-<number>/<id>/"
        )

    city = _city_before(segments, slug_index)
    if not city:
        raise FundaUrlUnparseable("could not identify the city segment")

    street, number, addition = _split_slug(segments[slug_index])
    listing_id = next((s for s in segments[slug_index + 1:] if LISTING_ID_RE.match(s)), None)

    return FundaAddress(
        street=street, house_number=number, addition=addition,
        city=_titlecase(city), listing_id=listing_id,
    )


def _is_bare_share_link(segments: list[str]) -> bool:
    """Funda's in-app share button hands out ``/detail/<id>?utm_source=...`` —
    a listing id and tracking parameters, nothing else. Every other Funda URL
    shape carries at least a route word alongside the id (``/detail/koop/...``,
    or the address slug itself); this one is only ever a route word plus a
    bare id, which is the signal that the address is not merely hard to find
    here — it was never in the URL to begin with.
    """
    remaining = [s for s in segments
                 if s not in ROUTE_WORDS and not LISTING_ID_RE.match(s)]
    return not remaining and any(LISTING_ID_RE.match(s) for s in segments)


def _address_slug_index(segments: list[str]) -> Optional[int]:
    """The segment holding the address: a dwelling type plus a trailing number."""
    for index, segment in enumerate(segments):
        head = segment.split("-")[0]
        if head in DWELLING_TYPES and re.search(r"\d", segment):
            return index
    return None


def _city_before(segments: list[str], slug_index: int) -> Optional[str]:
    """The nearest preceding segment that is not route structure."""
    for segment in reversed(segments[:slug_index]):
        if segment not in ROUTE_WORDS and not LISTING_ID_RE.match(segment):
            return segment
    return None


def _split_slug(slug: str) -> tuple[str, str, Optional[str]]:
    """'huis-singel-30-a' -> ('Singel', '30', 'a').

    The listing id sometimes sits between the type and the street in older
    URLs ('huis-43829102-singel-30'), so any long numeric token is dropped
    before the trailing house number is taken.
    """
    tokens = [t for t in slug.split("-") if t]
    if tokens and tokens[0] in DWELLING_TYPES:
        tokens = tokens[1:]
    tokens = [t for t in tokens if not LISTING_ID_RE.match(t)]

    addition: Optional[str] = None
    number: Optional[str] = None

    # Walk back from the end: an optional addition, then the number itself.
    while tokens:
        match = HOUSE_NUMBER_RE.match(tokens[-1])
        if match and match.group("number"):
            number = match.group("number")
            addition = match.group("addition") or addition
            tokens.pop()
            break
        if number is None and len(tokens[-1]) <= 6 and tokens[-1].isalnum():
            addition = tokens.pop()          # a bare addition like 'bis' or 'a'
            continue
        break

    if number is None or not tokens:
        raise FundaUrlUnparseable(f"could not split street and house number from {slug!r}")

    return _titlecase(" ".join(tokens)), number, addition


def _titlecase(slug: str) -> str:
    """'bos-en-vaartlaan' -> 'Bos en Vaartlaan'. Dutch tussenvoegsels stay lower."""
    small = {"en", "van", "de", "der", "den", "het", "op", "aan", "te", "ten", "ter"}
    words = slug.replace("-", " ").split()
    return " ".join(
        word if index and word in small else word.capitalize()
        for index, word in enumerate(words)
    )
