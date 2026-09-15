"""Orchestrator tests — the run loop wired to a real (in-memory) repository
and fake everything else. This is where the two guards are proven to work
together rather than only in isolation.
"""
from __future__ import annotations

from typing import AsyncIterator

import pytest

from app.core.config import settings
from app.db.repository import PropertyRepository
from app.models.enrichment import EnrichmentBundle, ProviderResult, ProviderStatus
from app.models.property import GeoIdentity, ListingSource, PropertyListing
from app.services.region_filter import parse_regions
from app.models.score import HolisticScore
from app.notifiers.base import NullNotifier
from app.pipeline.orchestrator import Orchestrator
from app.services.enrichment import GeocodeFailed

MEMORY = "sqlite+aiosqlite:///:memory:"


def listing(pid: str, price=500_000, area=100, city=None, postal_code="1015AA") -> PropertyListing:
    return PropertyListing(
        property_id=pid, source=ListingSource.HUISPEDIA, url=f"https://huispedia.nl/{pid}",
        address=f"Straat {pid}", postal_code=postal_code, house_number="1", city=city,
        price_eur=price, living_area_m2=area, construction_year=1965,
    )


def bundle(pid: str) -> EnrichmentBundle:
    layers = {n: ProviderResult(provider=n, status=ProviderStatus.MISSING)
              for n in EnrichmentBundle.LAYER_NAMES}
    return EnrichmentBundle(property_id=pid, geo=GeoIdentity(latitude=52.3, longitude=4.9), **layers)


class FakeScraper:
    source = ListingSource.HUISPEDIA

    def __init__(self, ids, listings=None):
        self.ids, self.listings = ids, listings or {}

    async def discover(self, **kw) -> AsyncIterator[PropertyListing]:
        for pid in self.ids:
            yield self.listings.get(pid) or listing(pid)


class FakeEnricher:
    def __init__(self, fail=()):
        self.fail, self.calls = set(fail), []

    async def enrich_one(self, listing, geo=None, only=None):
        self.calls.append(listing.property_id)
        if listing.property_id in self.fail:
            raise GeocodeFailed(f"{listing.property_id}: nope")
        return bundle(listing.property_id)


class FakeScorer:
    def __init__(self, score=8.0): self.score_value = score

    def score(self, b) -> HolisticScore:
        return HolisticScore(property_id=b.property_id, total_score=self.score_value,
                             confidence=1.0, data_coverage_pct=100.0, dimensions=[])


async def build(ids, *, score=8.0, fail_geocode=(), min_score=0.0):
    repo = PropertyRepository(MEMORY)
    await repo.init_schema()
    notifier = NullNotifier()
    orch = Orchestrator.__new__(Orchestrator)
    orch.http, orch.repo = None, repo
    orch.scrapers = [FakeScraper(ids)]
    orch.enricher = FakeEnricher(fail_geocode)
    orch.scorer = FakeScorer(score)
    orch.notifier = notifier
    return orch, repo, notifier


@pytest.fixture(autouse=True)
def low_threshold(monkeypatch):
    monkeypatch.setattr(settings, "min_score_to_notify", 0.0)
    monkeypatch.setattr(settings, "max_listings_per_cycle", 50)


# -- the loop ----------------------------------------------------------------


async def test_a_first_cycle_processes_and_alerts():
    orch, repo, notifier = await build(["a", "b"])
    sent = await orch.run_once()

    assert sent == 2
    assert len(notifier.sent) == 2
    assert (await repo.stats()) == {"properties": 2, "enriched": 2, "scored": 2, "notified": 2}
    await repo.close()


async def test_a_second_cycle_over_the_same_listings_sends_nothing():
    """The guard that makes the agent safe to leave running."""
    orch, repo, notifier = await build(["a", "b"])
    await orch.run_once()
    notifier.sent.clear()

    assert await orch.run_once() == 0
    assert notifier.sent == []
    assert (await repo.stats())["properties"] == 2
    await repo.close()


async def test_a_known_listing_is_not_re_enriched():
    """Deduplication must save the API calls, not just the alert."""
    orch, repo, _ = await build(["a"])
    await orch.run_once()
    orch.enricher.calls.clear()

    await orch.run_once()

    assert orch.enricher.calls == []
    await repo.close()


async def test_a_new_listing_among_known_ones_still_alerts():
    orch, repo, notifier = await build(["a"])
    await orch.run_once()
    notifier.sent.clear()
    orch.scrapers = [FakeScraper(["a", "b"])]

    assert await orch.run_once() == 1
    assert [s.listing.property_id for s in notifier.sent] == ["b"]
    await repo.close()


# -- thresholds and failures -------------------------------------------------


async def test_below_threshold_is_scored_but_not_sent(monkeypatch):
    monkeypatch.setattr(settings, "min_score_to_notify", 7.0)
    orch, repo, notifier = await build(["a"], score=4.0)

    assert await orch.run_once() == 0
    assert notifier.sent == []
    # Still stored, so raising the bar later does not lose the record.
    assert (await repo.stats())["scored"] == 1
    assert [l.property_id for l in await repo.list_by_status("scored")] == ["a"]
    await repo.close()


async def test_an_ungeocodable_listing_is_marked_failed_and_skipped():
    orch, repo, notifier = await build(["a", "b"], fail_geocode=["a"])
    sent = await orch.run_once()

    assert sent == 1
    assert [l.property_id for l in await repo.list_by_status("failed")] == ["a"]
    assert (await repo.stats())["enriched"] == 1
    await repo.close()


async def test_one_bad_listing_does_not_abort_the_cycle():
    orch, repo, notifier = await build(["a", "b", "c"], fail_geocode=["b"])

    assert await orch.run_once() == 2
    await repo.close()


class FailingNotifier(NullNotifier):
    async def send(self, item) -> bool:
        return False


async def test_a_failed_send_is_retried_on_a_later_cycle():
    """A transient Telegram outage must not lose the alert permanently.

    The property is already known by the next cycle, so the duplicate-processing
    guard skips it before the notify step — the retry has to come from the
    undelivered sweep, not from rediscovery.
    """
    orch, repo, _ = await build(["a"])
    orch.notifier = FailingNotifier()
    assert await orch.run_once() == 0
    assert await repo.is_notified("a") is False

    working = NullNotifier()
    orch.notifier = working
    assert await orch.run_once() == 1
    assert await repo.is_notified("a") is True
    assert [s.listing.property_id for s in working.sent] == ["a"]
    await repo.close()


async def test_the_retry_sweep_does_not_re_enrich():
    """The score is already stored; a retry is a local read plus a send."""
    orch, repo, _ = await build(["a"])
    orch.notifier = FailingNotifier()
    await orch.run_once()
    orch.enricher.calls.clear()

    orch.notifier = NullNotifier()
    await orch.run_once()

    assert orch.enricher.calls == []
    await repo.close()


async def test_the_sweep_respects_the_score_threshold(monkeypatch):
    monkeypatch.setattr(settings, "min_score_to_notify", 7.0)
    orch, repo, notifier = await build(["a"], score=4.0)
    await orch.run_once()

    # Never eligible, so the sweep must not pick it up on later cycles either.
    assert await orch.run_once() == 0
    assert notifier.sent == []
    await repo.close()


async def test_a_delivered_alert_is_never_swept_again():
    orch, repo, notifier = await build(["a"])
    await orch.run_once()
    notifier.sent.clear()

    assert await orch.run_once() == 0
    assert notifier.sent == []
    await repo.close()


# -- budget filters ----------------------------------------------------------


async def test_a_listing_over_budget_is_never_enriched(monkeypatch):
    """Enrichment costs a geocode plus six API calls; don't spend them."""
    monkeypatch.setattr(settings, "max_price_eur", 600_000)
    orch, repo, notifier = await build(["cheap", "dear"])
    orch.scrapers = [FakeScraper(["cheap", "dear"], {
        "cheap": listing("cheap", price=550_000),
        "dear": listing("dear", price=900_000),
    })]

    assert await orch.run_once() == 1
    assert orch.enricher.calls == ["cheap"]
    assert [s.listing.property_id for s in notifier.sent] == ["cheap"]
    await repo.close()


async def test_a_rejected_listing_is_recorded_as_ignored(monkeypatch):
    """So it is skipped for free next cycle rather than re-fetched forever."""
    monkeypatch.setattr(settings, "max_price_eur", 600_000)
    orch, repo, _ = await build(["dear"])
    orch.scrapers = [FakeScraper(["dear"], {"dear": listing("dear", price=900_000)})]
    await orch.run_once()

    assert [l.property_id for l in await repo.list_by_status("ignored")] == ["dear"]
    orch.enricher.calls.clear()
    await orch.run_once()
    assert orch.enricher.calls == []
    await repo.close()


async def test_too_small_is_filtered_on_area(monkeypatch):
    monkeypatch.setattr(settings, "min_living_area_m2", 90)
    orch, repo, _ = await build(["tiny"])
    orch.scrapers = [FakeScraper(["tiny"], {"tiny": listing("tiny", area=40)})]

    assert await orch.run_once() == 0
    await repo.close()


async def test_a_missing_price_is_kept_not_rejected(monkeypatch):
    """Absent is not the same as outside — do not lose a house to a feed gap."""
    monkeypatch.setattr(settings, "max_price_eur", 600_000)
    orch, repo, _ = await build(["unknown"])
    orch.scrapers = [FakeScraper(["unknown"], {"unknown": listing("unknown", price=None)})]

    assert await orch.run_once() == 1
    await repo.close()


async def test_no_filters_configured_lets_everything_through(monkeypatch):
    monkeypatch.setattr(settings, "max_price_eur", None)
    monkeypatch.setattr(settings, "min_living_area_m2", None)
    orch, repo, _ = await build(["a"])
    orch.scrapers = [FakeScraper(["a"], {"a": listing("a", price=5_000_000, area=10)})]

    assert await orch.run_once() == 1
    await repo.close()


# -- region filter -----------------------------------------------------------


async def test_a_listing_outside_the_region_is_filtered(monkeypatch):
    orch, repo, _ = await build(["out"])
    orch.region_filter = parse_regions("Amsterdam")
    orch.scrapers = [FakeScraper(["out"], {"out": listing("out", city="Almelo")})]

    assert await orch.run_once() == 0
    await repo.close()


async def test_a_listing_inside_the_region_is_kept(monkeypatch):
    orch, repo, _ = await build(["in"])
    orch.region_filter = parse_regions("Amsterdam")
    orch.scrapers = [FakeScraper(["in"], {"in": listing("in", city="Amsterdam")})]

    assert await orch.run_once() == 1
    await repo.close()


async def test_an_unset_region_filter_matches_everything(monkeypatch):
    """The historical behaviour, and what an existing daemon with no
    WOONAGENT_REGIONS must keep doing after this feature lands."""
    orch, repo, _ = await build(["anywhere"])
    orch.scrapers = [FakeScraper(["anywhere"], {"anywhere": listing("anywhere", city="Almelo")})]

    assert await orch.run_once() == 1
    await repo.close()


async def test_the_region_filter_is_the_first_check_run(monkeypatch):
    """It is the cheapest of the three — a string compare on data the scraper
    already has — so it should not wait behind price and area."""
    monkeypatch.setattr(settings, "max_price_eur", 100)  # would reject on price too
    orch, repo, _ = await build(["out"])
    orch.region_filter = parse_regions("Amsterdam")
    orch.scrapers = [FakeScraper(["out"], {"out": listing("out", city="Almelo", price=50)})]

    await orch.run_once()
    reason = orch._rejected_by_filters(listing("out", city="Almelo", price=50))
    assert "region" in reason
