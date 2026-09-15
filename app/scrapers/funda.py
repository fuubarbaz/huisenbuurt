"""Funda — not implemented, deliberately.

Funda does not serve a robots.txt. Requesting ``https://www.funda.nl/robots.txt``
returns 200 with ~14 KB of HTML: an anti-bot interstitial titled *"Je bent bijna
op de pagina die je zoekt"*. That is not a missing policy to be read as
permission — it is the site stating, through its bot-detection layer, that it
does not want automated traffic. Funda's terms of use say the same in words.

Scraping it anyway would mean defeating that detection: rotating User-Agents to
impersonate browsers, solving or side-stepping the challenge, and staying below
whatever heuristics they run. This module does none of that, and the earlier
User-Agent rotation in ``app.core.http_client`` was removed for the same reason
— see the note beside ``DEFAULT_USER_AGENT``.

``app.core.robots.RobotsGate`` enforces this in code rather than by convention:
it refuses any host whose /robots.txt is not parseable robots directives, so
Funda is blocked even if this module were reinstated.

Legitimate routes to Funda data, if you want it
-----------------------------------------------
* **Funda's own alerts.** A saved search with e-mail notification is free, and
  those mails can be forwarded into an inbox this agent reads. That is the
  intended interface for exactly this use case.
* **Funda Partners / the commercial feed.** Funda licenses listing data to
  brokers and portals under contract; that is the sanctioned bulk route.
* **The listing broker's own site.** NVM member offices publish their own
  listings, many with a feed, and most permit crawling.
* **Huispedia**, already implemented here, which permits crawlers in robots.txt
  and publishes sitemaps and schema.org JSON-LD.

If you obtain contractual access, add a scraper for that endpoint rather than
re-enabling this one.
"""
from __future__ import annotations

from typing import AsyncIterator

from app.models.property import ListingSource, PropertyListing
from app.scrapers.base import BaseScraper


class FundaAccessNotPermitted(RuntimeError):
    """Raised on any attempt to scrape Funda. See the module docstring."""


class FundaScraper(BaseScraper):
    """Placeholder that refuses rather than a scraper that misbehaves."""

    source = ListingSource.FUNDA

    async def discover(self, **filters) -> AsyncIterator[PropertyListing]:
        raise FundaAccessNotPermitted(
            "funda.nl serves an anti-bot page in place of robots.txt and its terms "
            "forbid automated access. Use a Funda e-mail alert, a licensed feed, or "
            "the broker's own site. See app/scrapers/funda.py for the options."
        )
        yield  # pragma: no cover — keeps this an async generator
