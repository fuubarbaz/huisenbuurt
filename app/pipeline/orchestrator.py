"""The Phase 1 run loop: scrape -> dedupe -> enrich -> score -> notify.

This is the only module that wires the layers together, and the only one that
knows the order of operations. Each layer it calls is independently testable
and independently callable from a FastAPI route.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

from app.core.config import settings
from app.core.http_client import HttpClient
from app.core.rate_limit import next_poll_interval
from app.db.repository import PropertyRepository
from app.models.property import PropertyListing
from app.models.score import ScoredProperty
from app.notifiers.base import Notifier, NullNotifier
from app.notifiers.telegram import TelegramNotifier
from app.scrapers import BaseScraper, HuispediaScraper
from app.services.region_filter import RegionFilter
from app.services.enrichment import EnrichmentPipeline, GeocodeFailed
from app.services.scoring import ScoringEngine

log = logging.getLogger(__name__)


def _no_progress(_message: str) -> None:
    """Default progress sink: the CLI has stdout and its logs."""


class Orchestrator:
    #: A class attribute, not only an instance one. Tests build this object
    #: with ``__new__`` to skip the network-touching constructor, so anything
    #: ``run_once`` relies on must have a working default without ``__init__``
    #: having run — otherwise adding a field here breaks them at a distance.
    _progress: Callable[[str], None] = staticmethod(_no_progress)
    #: Same reasoning as ``_progress`` above: tests build this object with
    #: ``__new__``, bypassing ``__init__``, so this must work unset.
    region_filter: RegionFilter = RegionFilter(frozenset(), frozenset())

    def __init__(
        self,
        http: HttpClient,
        repo: PropertyRepository,
        *,
        scrapers: list[BaseScraper] | None = None,
        notifier: Notifier | None = None,
        progress: Optional[Callable[[str], None]] = None,
        region_filter: Optional[RegionFilter] = None,
    ) -> None:
        self.http = http
        self.repo = repo
        # Left unset, the class default above stands — unfiltered, matching
        # the historical behaviour, so a daemon already running with no
        # WOONAGENT_REGIONS keeps watching every listing.
        if region_filter is not None:
            self.region_filter = region_filter
        # A cycle triggered from the browser is otherwise a spinner with
        # nothing behind it for several minutes. Left unset, the class default
        # above stands.
        if progress is not None:
            self._progress = progress
        # Huispedia only: see app/scrapers/funda.py for why Funda is excluded.
        self.scrapers = scrapers if scrapers is not None else [HuispediaScraper(http)]
        self.enricher = EnrichmentPipeline(http)
        self.scorer = ScoringEngine()
        self.notifier = notifier or (
            NullNotifier() if settings.dry_run else TelegramNotifier(http)
        )

    def _rejected_by_filters(self, listing: PropertyListing) -> Optional[str]:
        """Why this listing is not worth enriching, or None if it is.

        Enrichment costs a geocode plus six API calls, so a listing outside the
        budget is dropped here rather than scored and then hidden. A listing
        with no price or no floor area is kept: absent is not the same as
        outside, and a promising house should not be lost to a gap in the feed.

        The region check comes first and is the cheapest of the three — a
        string compare against the city and postcode Huispedia's URL already
        gave us, before price or area are even read.
        """
        if not self.region_filter.matches(listing):
            return f"outside the configured region ({self.region_filter})"
        if (cap := settings.max_price_eur) and listing.price_eur and listing.price_eur > cap:
            return f"price {listing.price_eur} above the {cap} cap"
        floor = settings.min_living_area_m2
        if floor and listing.living_area_m2 and listing.living_area_m2 < floor:
            return f"living area {listing.living_area_m2} m² below the {floor} m² floor"
        return None

    async def run_once(self) -> int:
        """One full cycle: ingest what is new, then retry what never landed."""
        new = await self._process_new()
        self._progress("retrying any undelivered alerts")
        return new + await self._retry_undelivered()

    async def _retry_undelivered(self) -> int:
        """Deliver scored properties whose alert never got through.

        Without this, a transient Telegram outage loses the alert for good: the
        property is known by the next cycle, so the duplicate-processing guard
        skips it long before the notify step. Re-scoring is not needed — the
        score is already stored — so this is a cheap, purely local sweep.
        """
        pending = await self.repo.pending_notification(settings.min_score_to_notify)
        sent = 0
        for property_id in pending:
            listing = await self.repo.get_listing(property_id)
            bundle = await self.repo.get_enrichment(property_id)
            if listing is None:
                continue
            score = self.scorer.score(bundle) if bundle is not None else None
            ok = await self.notifier.send(
                ScoredProperty(listing=listing, enrichment=bundle, score=score)
            )
            await self.repo.mark_notified(property_id, success=ok)
            if ok:
                await self.repo.set_status(property_id, "notified")
                sent += 1
                log.info("delivered a previously undelivered alert for %s", property_id)
        return sent

    async def _process_new(self) -> int:
        """Scrape, dedupe, enrich, score and alert on genuinely new listings."""
        sent = 0
        if not self.region_filter.is_empty:
            self._progress(f"region filter active: {self.region_filter}")
        for scraper in self.scrapers:
            self._progress(f"discovering new listings from {type(scraper).__name__}")
            async for listing in scraper.discover(max_listings=settings.max_listings_per_cycle):
                # upsert_listing returns False for an already-known property:
                # that single check is what prevents duplicate work and alerts.
                if not await self.repo.upsert_listing(listing):
                    continue
                self._progress(f"new listing {listing.address}")

                # Budget filters are applied after the row is recorded, not
                # before: a rejected listing is then a *known* listing, so it
                # is skipped for free on every later cycle instead of being
                # re-fetched and re-rejected forever.
                if (reason := self._rejected_by_filters(listing)) is not None:
                    log.debug("ignoring %s: %s", listing.property_id, reason)
                    await self.repo.set_status(listing.property_id, "ignored")
                    continue
                try:
                    bundle = await self.enricher.enrich_one(
                        listing, geo=await self.repo.cached_geo(listing.property_id)
                    )
                except GeocodeFailed as exc:
                    log.warning("geocode failed: %s", exc)
                    await self.repo.set_status(listing.property_id, "failed")
                    continue

                await self.repo.save_enrichment(bundle)
                await self.repo.set_status(listing.property_id, "enriched")

                score = self.scorer.score(bundle)
                await self.repo.save_score(score)
                self._progress(f"scored {listing.address} at {score.total_score}")

                if score.total_score < settings.min_score_to_notify:
                    await self.repo.set_status(listing.property_id, "scored")
                    continue
                if await self.repo.is_notified(listing.property_id):
                    continue

                ok = await self.notifier.send(
                    ScoredProperty(listing=listing, enrichment=bundle, score=score)
                )
                await self.repo.mark_notified(listing.property_id, success=ok)
                await self.repo.set_status(listing.property_id, "notified" if ok else "failed")
                sent += int(ok)
        return sent

    async def run_forever(self) -> None:
        """Poll on a randomised 5-15 minute interval, indefinitely."""
        while True:
            try:
                alerts = await self.run_once()
                log.info("cycle complete, %d alert(s) sent", alerts)
            except Exception:  # noqa: BLE001 — the loop must survive one bad cycle
                log.exception("cycle failed; continuing")
            delay = next_poll_interval(
                settings.scrape_interval_min_seconds, settings.scrape_interval_max_seconds
            )
            log.info("sleeping %.0fs until next cycle", delay)
            await asyncio.sleep(delay)
