"""Enrichment pipeline: turn one listing into one fully-enriched location record.

Design notes
------------
* **Fan-out, not a chain.** The six open-data layers are mutually independent,
  so they run concurrently. Wall-clock cost is the slowest provider, not the
  sum. Only the geocode step is sequential, because every layer is keyed on
  its output.
* **Degrade, never abort.** Each provider is wrapped by ``BaseProvider.run``,
  which converts timeouts and upstream failures into a MISSING/ERROR result.
  A bundle with four healthy layers is still worth scoring; the scoring engine
  reads ``coverage_pct`` to decide how much to trust it.
* **No side effects.** This module reads APIs and returns a Pydantic model. It
  does not write to the database and does not notify anyone. That separation
  is what lets a FastAPI route in Phase 2 call ``enrich_one`` directly and
  serialise the return value straight to JSON.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Iterable, Optional, Sequence

from app.core.config import settings
from app.core.http_client import HttpClient
from app.models.enrichment import (
    CrimeStats,
    DemographicsData,
    EducationData,
    EnrichmentBundle,
    LeefbaarometerData,
    AirQualityData,
    NoiseData,
    ProviderResult,
    ProviderStatus,
    SoilRiskData,
)
from app.models.property import GeoIdentity, PropertyListing
from app.services.enrichment.providers import (
    BaseProvider,
    CBSDemographicsProvider,
    DUOEducationProvider,
    EnrichmentContext,
    LeefbaarometerProvider,
    PolitieProvider,
    RIVMAirQualityProvider,
    RIVMNoiseProvider,
    SoilRiskProvider,
)
from app.services.geo.pdok_locatieserver import GeocodeError, PDOKLocatieserver

log = logging.getLogger(__name__)


class GeocodeFailed(Exception):
    """A listing that cannot be geocoded cannot be enriched at all."""


class EnrichmentPipeline:
    """Orchestrates geocoding + the six enrichment layers.

    Construct once per process and reuse — it holds a pooled HTTP client.

        async with HttpClient() as http:
            pipeline = EnrichmentPipeline(http)
            bundle = await pipeline.enrich_one(listing)
    """

    #: Bundle field name -> provider class. Adding a layer means adding a line
    #: here, a payload model, and a provider module. Nothing else changes.
    PROVIDER_MAP: dict[str, type[BaseProvider]] = {
        "leefbaarometer": LeefbaarometerProvider,
        "crime": PolitieProvider,
        "demographics": CBSDemographicsProvider,
        "education": DUOEducationProvider,
        "noise": RIVMNoiseProvider,
        "air": RIVMAirQualityProvider,
        "soil": SoilRiskProvider,
    }

    #: Empty-result factories, used when a layer never got a chance to run.
    _EMPTY_PAYLOAD_TYPES = {
        "leefbaarometer": LeefbaarometerData,
        "crime": CrimeStats,
        "demographics": DemographicsData,
        "education": EducationData,
        "noise": NoiseData,
        "air": AirQualityData,
        "soil": SoilRiskData,
    }

    def __init__(
        self,
        http: HttpClient,
        *,
        geocoder: Optional[PDOKLocatieserver] = None,
        providers: Optional[dict[str, BaseProvider]] = None,
        provider_timeout: Optional[float] = None,
        max_concurrency: Optional[int] = None,
    ) -> None:
        self.http = http
        self.geocoder = geocoder or PDOKLocatieserver(http)
        self.providers: dict[str, BaseProvider] = providers or {
            key: cls(http) for key, cls in self.PROVIDER_MAP.items()
        }
        self.provider_timeout = provider_timeout or settings.enrichment_provider_timeout_seconds
        self._semaphore = asyncio.Semaphore(max_concurrency or settings.enrichment_max_concurrency)

    # -- public API --------------------------------------------------------

    async def enrich_one(
        self,
        listing: PropertyListing,
        *,
        geo: Optional[GeoIdentity] = None,
        only: Optional[Sequence[str]] = None,
    ) -> EnrichmentBundle:
        """Enrich a single listing.

        Args:
            listing: The scraped property.
            geo: A previously resolved identity, to skip the geocode round-trip
                (the DB caches these — an address's coordinates never change).
            only: Restrict the run to these layer names. Unlisted layers come
                back SKIPPED. Useful for cheap partial refreshes.

        Returns:
            A bundle where every layer is present, successful or not.

        Raises:
            GeocodeFailed: The address could not be resolved, so no layer can
                be keyed. This is the one genuinely fatal condition.
        """
        started = time.perf_counter()

        if geo is None:
            try:
                geo = await self.geocoder.resolve(
                    postal_code=listing.postal_code,
                    house_number=listing.house_number,
                    addition=listing.house_number_addition,
                )
            except GeocodeError as exc:
                raise GeocodeFailed(f"{listing.property_id}: {exc}") from exc

        ctx = EnrichmentContext(listing=listing, geo=geo)
        selected = set(only) if only is not None else set(self.providers)

        results = await self._run_providers(ctx, selected)

        bundle = EnrichmentBundle(
            property_id=listing.property_id,
            geo=geo,
            total_duration_ms=int((time.perf_counter() - started) * 1000),
            **results,
        )

        failed = bundle.failures()
        if failed:
            log.info(
                "enriched %s with %.0f%% coverage in %dms; degraded layers: %s",
                listing.property_id, bundle.coverage_pct, bundle.total_duration_ms, failed,
            )
        else:
            log.info(
                "enriched %s fully in %dms", listing.property_id, bundle.total_duration_ms
            )
        return bundle

    async def enrich_many(
        self,
        listings: Iterable[PropertyListing],
        *,
        max_parallel_properties: int = 3,
    ) -> list[EnrichmentBundle]:
        """Enrich a batch, bounding how many properties are in flight at once.

        Listings that fail to geocode are dropped with a warning rather than
        failing the batch — a single unparseable address should not cost you
        the rest of the morning's new listings.
        """
        gate = asyncio.Semaphore(max_parallel_properties)

        async def _one(listing: PropertyListing) -> Optional[EnrichmentBundle]:
            async with gate:
                try:
                    return await self.enrich_one(listing)
                except GeocodeFailed as exc:
                    log.warning("skipping un-geocodable listing: %s", exc)
                    return None

        bundles = await asyncio.gather(*(_one(item) for item in listings))
        return [b for b in bundles if b is not None]

    # -- internals ---------------------------------------------------------

    async def _run_providers(
        self, ctx: EnrichmentContext, selected: set[str]
    ) -> dict[str, ProviderResult]:
        """Fan out to every selected provider concurrently.

        ``BaseProvider.run`` already swallows provider-level exceptions, so a
        gather here cannot raise; the ``return_exceptions`` guard covers only
        the pathological case of a bug in the wrapper itself.
        """

        async def _guarded(key: str, provider: BaseProvider) -> tuple[str, ProviderResult]:
            async with self._semaphore:
                return key, await provider.run(ctx, timeout=self.provider_timeout)

        tasks = [
            _guarded(key, provider)
            for key, provider in self.providers.items()
            if key in selected
        ]
        settled = await asyncio.gather(*tasks, return_exceptions=True)

        results: dict[str, ProviderResult] = {}
        for item in settled:
            if isinstance(item, BaseException):
                log.error("provider wrapper raised unexpectedly: %s", item)
                continue
            key, result = item
            results[key] = result

        # Backfill every layer the run did not produce, so the bundle shape is
        # always complete and callers never have to check for absence.
        for key in self._EMPTY_PAYLOAD_TYPES:
            if key not in results:
                results[key] = self._placeholder(key, selected)
        return results

    def _placeholder(self, key: str, selected: set[str]) -> ProviderResult:
        deselected = key not in selected
        return ProviderResult(
            provider=key,
            status=ProviderStatus.SKIPPED if deselected else ProviderStatus.ERROR,
            error=None if deselected else "provider did not report a result",
        )


async def enrich_listing(listing: PropertyListing) -> EnrichmentBundle:
    """One-shot convenience wrapper for scripts and tests.

    Opens and closes its own HTTP client, so prefer the class directly in any
    long-running process or FastAPI app where the pool should be shared.
    """
    async with HttpClient() as http:
        return await EnrichmentPipeline(http).enrich_one(listing)
