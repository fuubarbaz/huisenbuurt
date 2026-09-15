"""robots.txt compliance, enforced rather than assumed.

Every scraper fetch goes through :meth:`RobotsGate.allows`. The rules are
fetched once per host and cached for the process lifetime.

A note on failure modes, because they are a real decision and not a detail:

* **Parsed, and disallows the path** — refuse. No override exists.
* **Parsed, and allows it** — proceed.
* **Not a robots.txt at all** — refuse. Some sites answer /robots.txt with an
  HTML anti-bot interstitial (funda.nl does, at time of writing). That is not
  an absent policy, it is a site telling you it does not want automated
  traffic, and treating it as "no rules found" would be wilful misreading.
* **Network error** — refuse, and log. Fail closed: an unreachable policy is
  not permission.
"""
from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

from app.core.http_client import DEFAULT_USER_AGENT, HttpClient

log = logging.getLogger(__name__)

#: A body that does not look like a robots.txt is treated as a refusal.
_ROBOTS_DIRECTIVES = ("user-agent:", "disallow:", "allow:", "sitemap:")


class RobotsDisallowed(Exception):
    """The site's robots.txt does not permit fetching this URL."""


class RobotsGate:
    """Per-host robots.txt cache and check."""

    def __init__(self, http: HttpClient, user_agent: str = DEFAULT_USER_AGENT) -> None:
        self.http = http
        self.user_agent = user_agent
        self._cache: dict[str, Optional[RobotFileParser]] = {}

    async def _rules_for(self, url: str) -> Optional[RobotFileParser]:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin in self._cache:
            return self._cache[origin]

        parser: Optional[RobotFileParser] = None
        try:
            body = await self.http.get_text(f"{origin}/robots.txt", max_retries=1)
        except Exception as exc:  # noqa: BLE001 — unreachable policy is not permission
            log.warning("robots.txt unreachable for %s (%s); refusing", origin, exc)
        else:
            lowered = body.lower()
            if any(directive in lowered for directive in _ROBOTS_DIRECTIVES):
                parser = RobotFileParser()
                parser.parse(body.splitlines())
            else:
                log.warning(
                    "%s/robots.txt returned %d bytes that are not robots directives "
                    "(likely an anti-bot page); refusing", origin, len(body),
                )

        self._cache[origin] = parser
        return parser

    async def allows(self, url: str) -> bool:
        rules = await self._rules_for(url)
        return bool(rules and rules.can_fetch(self.user_agent, url))

    async def require(self, url: str) -> None:
        """Raise :class:`RobotsDisallowed` unless the fetch is permitted."""
        if not await self.allows(url):
            raise RobotsDisallowed(f"robots.txt does not permit fetching {url}")

    async def sitemaps(self, url: str) -> list[str]:
        """Sitemap URLs the host advertises, if any."""
        rules = await self._rules_for(url)
        if rules is None:
            return []
        # RobotFileParser.site_maps() is a method and returns None when the
        # file advertises none.
        return list(rules.site_maps() or [])
