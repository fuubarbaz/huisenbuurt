"""Shared async HTTP client: exponential backoff with jitter, UA rotation,
per-host rate limiting, and a single pooled connection.

Every network call in the app goes through this — scrapers and open-data
providers alike — so retry policy exists in exactly one place.
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Mapping, Optional

import httpx

from app.core.config import settings
from app.core.rate_limit import RateLimiter

log = logging.getLogger(__name__)

#: The agent identifies itself honestly and consistently.
#:
#: An earlier draft rotated through a pool of browser User-Agents, on the
#: theory that it would avoid being blocked. That was dropped deliberately.
#: Rotation only helps against a site that does not want automated traffic,
#: and this agent does not visit those: the one listing source it reads
#: publishes a robots.txt that permits it, and the compliance check in
#: ``app.core.robots`` enforces that. Against a site that permits you,
#: impersonating five different browsers is pointless and dishonest; a stable,
#: contactable identity is what lets an operator see you in their logs and
#: get in touch instead of silently blocking you.
DEFAULT_USER_AGENT = (
    "WoonAgent/0.1 (+personal house-search monitor; respects robots.txt)"
)

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


class TransientHTTPError(Exception):
    """Raised when all retries are exhausted on a retryable failure."""


class HttpClient:
    """Thin wrapper over httpx.AsyncClient. Use as an async context manager,
    or share one instance for the lifetime of the process."""

    def __init__(
        self,
        *,
        timeout: float | None = None,
        rate_limiter: RateLimiter | None = None,
        contact_email: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        self._timeout = timeout or settings.http_timeout_seconds
        self._limiter = rate_limiter or RateLimiter()
        self._contact = contact_email or settings.contact_email
        self._user_agent = user_agent or settings.user_agent or DEFAULT_USER_AGENT
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "HttpClient":
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout),
                follow_redirects=True,
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
                http2=False,
            )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _headers(self, extra: Mapping[str, str] | None) -> dict[str, str]:
        headers = {
            "User-Agent": self._user_agent,
            "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
            "Accept": "*/*",
        }
        # Identifying yourself is the polite convention for Dutch open-data APIs.
        if self._contact:
            headers["From"] = self._contact
        if extra:
            headers.update(extra)
        return headers

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any | None = None,
        headers: Mapping[str, str] | None = None,
        max_retries: int | None = None,
    ) -> httpx.Response:
        if self._client is None:
            await self.start()
        assert self._client is not None

        retries = settings.max_retries if max_retries is None else max_retries
        last_error: Exception | None = None

        for attempt in range(retries + 1):
            await self._limiter.acquire(url)
            try:
                resp = await self._client.request(
                    method, url, params=params, json=json, headers=self._headers(headers)
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                log.warning("%s %s attempt %d/%d failed: %s", method, url, attempt + 1, retries + 1, exc)
            else:
                if resp.status_code not in RETRY_STATUS:
                    resp.raise_for_status()
                    return resp
                last_error = httpx.HTTPStatusError(
                    f"HTTP {resp.status_code}", request=resp.request, response=resp
                )
                # Honour Retry-After when the server sends one.
                retry_after = _parse_retry_after(resp)
                if retry_after is not None and attempt < retries:
                    await asyncio.sleep(min(retry_after, settings.backoff_max_seconds))
                    continue

            if attempt < retries:
                await asyncio.sleep(self._backoff(attempt))

        raise TransientHTTPError(f"{method} {url} failed after {retries + 1} attempts: {last_error}")

    @staticmethod
    def _backoff(attempt: int) -> float:
        """Exponential backoff with full jitter."""
        ceiling = min(settings.backoff_base_seconds * (2 ** attempt), settings.backoff_max_seconds)
        return random.uniform(0.0, ceiling)

    async def get_json(self, url: str, **kwargs: Any) -> Any:
        resp = await self.request("GET", url, **kwargs)
        return resp.json()

    async def get_text(self, url: str, **kwargs: Any) -> str:
        resp = await self.request("GET", url, **kwargs)
        return resp.text


def _parse_retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None
