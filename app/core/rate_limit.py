"""Per-host token-bucket limiter plus randomised human-ish delays.

Dutch listing sites sit behind aggressive WAFs; open-data APIs do not. Limits
are therefore configured per host rather than globally, so a slow Funda poll
never throttles the PDOK lookups.
"""
from __future__ import annotations

import asyncio
import random
import time
from collections import defaultdict
from dataclasses import dataclass, field
from urllib.parse import urlparse


@dataclass
class HostBudget:
    """Requests-per-second budget for one host."""

    rate_per_second: float
    burst: int = 1
    _tokens: float = field(default=0.0, init=False)
    _updated: float = field(default_factory=time.monotonic, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    def __post_init__(self) -> None:
        self._tokens = float(self.burst)

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(self.burst, self._tokens + (now - self._updated) * self.rate_per_second)
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                await asyncio.sleep((1.0 - self._tokens) / self.rate_per_second)


# Conservative defaults. Scraper targets get a fraction of a request/second;
# public open-data APIs are allowed to run much hotter.
DEFAULT_BUDGETS: dict[str, HostBudget] = {
    "funda.nl": HostBudget(rate_per_second=0.15, burst=1),
    "huispedia.nl": HostBudget(rate_per_second=0.25, burst=1),
    # PDOK's geocoder is the one host this codebase hits in bulk, during a
    # school-index build. Measured at 166 req/s with no throttling and no
    # Retry-After, so the generic 5/s budget was the binding constraint, not
    # the service. 20/s is a small fraction of what it serves while still
    # leaving a wide margin.
    "pdok.nl": HostBudget(rate_per_second=20.0, burst=40),
    # Shared community routing services. Kept deliberately slow: they are run
    # on donated capacity and this agent is a guest on them.
    "project-osrm.org": HostBudget(rate_per_second=1.0, burst=3),
    "brouter.de": HostBudget(rate_per_second=1.0, burst=3),
}
_OPEN_DATA_DEFAULT = HostBudget(rate_per_second=5.0, burst=10)


class RateLimiter:
    def __init__(self, budgets: dict[str, HostBudget] | None = None) -> None:
        self._budgets: dict[str, HostBudget] = dict(budgets or DEFAULT_BUDGETS)
        self._fallback: dict[str, HostBudget] = defaultdict(
            lambda: HostBudget(_OPEN_DATA_DEFAULT.rate_per_second, _OPEN_DATA_DEFAULT.burst)
        )

    def _budget_for(self, url: str) -> HostBudget:
        host = (urlparse(url).hostname or "").lower()
        for suffix, budget in self._budgets.items():
            if host == suffix or host.endswith("." + suffix):
                return budget
        return self._fallback[host]

    async def acquire(self, url: str) -> None:
        await self._budget_for(url).acquire()


async def human_delay(min_s: float, max_s: float) -> None:
    """Jittered pause between page fetches, for scraper targets only."""
    await asyncio.sleep(random.uniform(min_s, max_s))


def next_poll_interval(min_s: int, max_s: int) -> float:
    """Randomised 5-15 minute-style interval between scrape cycles."""
    return random.uniform(min_s, max_s)
