"""How many requests this API will answer per client, per minute.

The outbound limiter in ``app/core/rate_limit.py`` protects other people's
infrastructure from this agent. This one protects the operator's wallet from
whoever is calling this agent: on Fly, a machine that never goes idle never
scales to zero, and ``/score`` alone is roughly a dozen calls to PDOK, CBS, the
BAG and two routing servers, so sustained hammering is real, billable cost —
not merely bad manners the way it would be against a scraped site.

A token bucket per client, same shape as the outbound one, but it REJECTS with
429 instead of waiting: an inbound caller should be told to slow down, not
silently delayed until the request looks like it hung.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class _Bucket:
    tokens: float
    updated: float = field(default_factory=time.monotonic)


class InboundRateLimiter:
    """Per-key token bucket, in-memory. Deliberately not shared across
    machines — this deployment runs exactly one, by design (see fly.toml)."""

    def __init__(self, rate_per_minute: float, burst: int) -> None:
        self.rate_per_second = rate_per_minute / 60.0
        self.burst = burst
        self._buckets: dict[str, _Bucket] = {}
        #: Bounds memory against an attacker cycling through many fake keys;
        #: real usage here is one operator, so this is a generous ceiling.
        self._max_keys = 500

    def allow(self, key: str) -> tuple[bool, float]:
        """Returns (allowed, retry_after_seconds — 0 when allowed)."""
        now = time.monotonic()
        bucket = self._buckets.get(key)
        if bucket is None:
            if len(self._buckets) >= self._max_keys:
                # Evict the stalest entry rather than grow unbounded.
                oldest = min(self._buckets, key=lambda k: self._buckets[k].updated)
                del self._buckets[oldest]
            # `now`, not the dataclass default_factory's own clock read: that
            # factory fires strictly after `now` was captured above, which
            # made a fresh bucket's `updated` timestamp land AFTER `now` and
            # produced a negative elapsed time on its very first use — enough
            # to shave a brand-new bucket below its own full burst and
            # occasionally deny the very first request from a new caller.
            bucket = _Bucket(tokens=float(self.burst), updated=now)
            self._buckets[key] = bucket

        bucket.tokens = min(self.burst, bucket.tokens + (now - bucket.updated) * self.rate_per_second)
        bucket.updated = now

        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return True, 0.0
        return False, round((1.0 - bucket.tokens) / self.rate_per_second, 1)
