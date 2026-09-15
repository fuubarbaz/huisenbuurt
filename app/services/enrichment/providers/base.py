"""Common contract for every open-data layer.

A provider knows one upstream API and nothing else. It does not know about the
database, the scoring weights, or Telegram. It receives an immutable context,
returns a ProviderResult, and never raises: the base class converts any
exception into ProviderStatus.ERROR so one dead API cannot abort a run.
"""
from __future__ import annotations

import abc
import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Generic, Optional, TypeVar

from pydantic import BaseModel

from app.core.http_client import HttpClient
from app.models.enrichment import ProviderResult, ProviderStatus
from app.models.property import GeoIdentity, PropertyListing

log = logging.getLogger(__name__)

PayloadT = TypeVar("PayloadT", bound=BaseModel)


@dataclass(frozen=True)
class EnrichmentContext:
    """Everything a provider is allowed to see about the property."""

    listing: PropertyListing
    geo: GeoIdentity

    @property
    def buurtcode(self) -> Optional[str]:
        return self.geo.buurtcode

    @property
    def gemeentecode(self) -> Optional[str]:
        return self.geo.gemeentecode

    @property
    def lat_lon(self) -> tuple[float, float]:
        return (self.geo.latitude, self.geo.longitude)


class SkipProvider(Exception):
    """Raise from fetch() when the layer does not apply to this property."""


class NoDataFound(Exception):
    """Raise from fetch() when upstream responded but had nothing for this location."""


class BaseProvider(abc.ABC, Generic[PayloadT]):
    """Subclasses implement :meth:`fetch` only."""

    name: str = "base"
    source_url: Optional[str] = None

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    @abc.abstractmethod
    async def fetch(self, ctx: EnrichmentContext) -> PayloadT:
        """Query the upstream API and return a validated payload.

        Raise :class:`SkipProvider` or :class:`NoDataFound` for the expected
        non-results; let anything else propagate — ``run`` will capture it.
        """

    async def run(self, ctx: EnrichmentContext, timeout: float) -> ProviderResult[PayloadT]:
        """Timed, timeout-bounded, exception-safe wrapper around ``fetch``."""
        started = time.perf_counter()

        def _result(status: ProviderStatus, payload=None, error=None) -> ProviderResult:
            return ProviderResult(
                provider=self.name,
                status=status,
                payload=payload,
                error=error,
                source_url=self.source_url,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

        try:
            payload = await asyncio.wait_for(self.fetch(ctx), timeout=timeout)
        except SkipProvider as exc:
            return _result(ProviderStatus.SKIPPED, error=str(exc) or None)
        except NoDataFound as exc:
            return _result(ProviderStatus.MISSING, error=str(exc) or None)
        except asyncio.TimeoutError:
            log.warning("provider %s timed out after %.1fs", self.name, timeout)
            return _result(ProviderStatus.ERROR, error=f"timeout after {timeout:.0f}s")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — deliberate catch-all boundary
            log.warning("provider %s failed: %s", self.name, exc, exc_info=log.isEnabledFor(logging.DEBUG))
            return _result(ProviderStatus.ERROR, error=f"{type(exc).__name__}: {exc}")

        if payload is None:
            return _result(ProviderStatus.MISSING, error="provider returned no payload")

        status = ProviderStatus.PARTIAL if self._is_partial(payload) else ProviderStatus.OK
        return _result(status, payload=payload)

    @staticmethod
    def _is_partial(payload: BaseModel) -> bool:
        """A payload with any unset top-level scalar is reported as PARTIAL."""
        values = [v for v in payload.model_dump().values() if not isinstance(v, (list, dict))]
        return any(v is None for v in values)
