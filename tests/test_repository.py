"""Repository tests — in-memory SQLite, no fixtures on disk.

The two guards are the point: duplicate *processing* and duplicate *alerting*
are separate, and conflating them either re-alerts every cycle or blocks
re-scoring forever.
"""
from __future__ import annotations

import pytest

from app.db.repository import PropertyRepository, sqlite_path
from app.models.enrichment import EnrichmentBundle, ProviderResult, ProviderStatus
from app.models.property import GeoIdentity, ListingSource, PropertyListing
from app.models.score import DimensionScore, HolisticScore, RiskFlag, ScoreDimension

MEMORY = "sqlite+aiosqlite:///:memory:"


def listing(pid="p1", price=500_000, url="https://huispedia.nl/x") -> PropertyListing:
    return PropertyListing(
        property_id=pid, source=ListingSource.HUISPEDIA, url=url,
        address="Teststraat 1", postal_code="1015AA", house_number="1",
        price_eur=price, living_area_m2=90, construction_year=1965,
    )


@pytest.fixture
async def repo():
    async with PropertyRepository(MEMORY) as r:
        yield r


def empty_bundle(pid="p1") -> EnrichmentBundle:
    layers = {n: ProviderResult(provider=n, status=ProviderStatus.MISSING)
              for n in EnrichmentBundle.LAYER_NAMES}
    return EnrichmentBundle(property_id=pid, geo=GeoIdentity(
        latitude=52.3, longitude=4.9, buurtcode="BU1", gemeentecode="GM0363"), **layers)


# -- URL handling ------------------------------------------------------------


def test_sqlalchemy_style_url_is_reduced_to_a_path():
    assert sqlite_path("sqlite+aiosqlite:///./data/woonagent.db") == "./data/woonagent.db"
    assert sqlite_path("sqlite:////tmp/x.db") == "/tmp/x.db"
    assert sqlite_path("sqlite+aiosqlite:///:memory:") == ":memory:"


# -- duplicate-processing guard ----------------------------------------------


async def test_first_insert_is_new_and_the_second_is_not(repo):
    assert await repo.upsert_listing(listing()) is True
    assert await repo.upsert_listing(listing()) is False


async def test_distinct_ids_are_both_new(repo):
    assert await repo.upsert_listing(listing("p1")) is True
    assert await repo.upsert_listing(listing("p2")) is True


async def test_reseeing_a_listing_refreshes_price_without_duplicating(repo):
    await repo.upsert_listing(listing(price=500_000))
    await repo.upsert_listing(listing(price=475_000))

    assert (await repo.get_listing("p1")).price_eur == 475_000
    assert (await repo.stats())["properties"] == 1


async def test_a_null_price_does_not_erase_a_known_one(repo):
    await repo.upsert_listing(listing(price=500_000))
    await repo.upsert_listing(listing(price=None))

    assert (await repo.get_listing("p1")).price_eur == 500_000


# -- duplicate-alert guard ---------------------------------------------------


async def test_unnotified_property_reports_false(repo):
    await repo.upsert_listing(listing())
    assert await repo.is_notified("p1") is False


async def test_a_failed_send_leaves_the_retry_open(repo):
    """The critical one: a delivery failure must not silence the next cycle."""
    await repo.upsert_listing(listing())
    await repo.mark_notified("p1", success=False)

    assert await repo.is_notified("p1") is False


async def test_a_successful_send_closes_it(repo):
    await repo.upsert_listing(listing())
    await repo.mark_notified("p1", success=True)

    assert await repo.is_notified("p1") is True


async def test_retry_after_failure_can_succeed(repo):
    await repo.upsert_listing(listing())
    await repo.mark_notified("p1", success=False)
    await repo.mark_notified("p1", success=True)

    assert await repo.is_notified("p1") is True


async def test_channels_are_tracked_separately(repo):
    await repo.upsert_listing(listing())
    await repo.mark_notified("p1", channel="telegram", success=True)

    assert await repo.is_notified("p1", "telegram") is True
    assert await repo.is_notified("p1", "email") is False


# -- geo cache ---------------------------------------------------------------


async def test_geo_round_trips(repo):
    await repo.upsert_listing(listing())
    await repo.save_geo("p1", GeoIdentity(latitude=52.37, longitude=4.89,
                                          buurtcode="BU0363AC01", gemeentecode="GM0363"))
    cached = await repo.cached_geo("p1")

    assert cached.buurtcode == "BU0363AC01"
    assert cached.latitude == pytest.approx(52.37)


async def test_no_geo_yet_returns_none(repo):
    await repo.upsert_listing(listing())
    assert await repo.cached_geo("p1") is None


async def test_saving_enrichment_also_caches_the_geocode(repo):
    """So a re-run never pays for the same PDOK lookup twice."""
    await repo.upsert_listing(listing())
    await repo.save_enrichment(empty_bundle())

    assert (await repo.cached_geo("p1")).buurtcode == "BU1"


# -- enrichment and scores ---------------------------------------------------


async def test_enrichment_bundle_round_trips(repo):
    await repo.upsert_listing(listing())
    await repo.save_enrichment(empty_bundle())
    back = await repo.get_enrichment("p1")

    assert back.property_id == "p1"
    assert back.coverage_pct == 0.0


async def test_scores_are_stored_and_ranked(repo):
    for pid, total in [("p1", 4.2), ("p2", 8.1), ("p3", 6.5)]:
        await repo.upsert_listing(listing(pid))
        await repo.save_score(HolisticScore(
            property_id=pid, total_score=total, confidence=1.0, data_coverage_pct=100.0,
            dimensions=[DimensionScore(dimension=ScoreDimension.SAFETY, score=total, weight=1.0)],
            risk_flags=[RiskFlag(code="X", severity="info", message="m")]))

    top = await repo.top_scored(limit=2)
    assert [r["property_id"] for r in top] == ["p2", "p3"]


async def test_min_score_filters_the_shortlist(repo):
    await repo.upsert_listing(listing("p1"))
    await repo.save_score(HolisticScore(property_id="p1", total_score=3.0, confidence=1.0,
                                        data_coverage_pct=50.0, dimensions=[]))

    assert await repo.top_scored(min_score=6.0) == []


# -- status ------------------------------------------------------------------


async def test_status_transitions_and_listing_by_status(repo):
    await repo.upsert_listing(listing())
    await repo.set_status("p1", "scored")

    assert [l.property_id for l in await repo.list_by_status("scored")] == ["p1"]
    assert await repo.list_by_status("new") == []


async def test_an_unknown_status_is_rejected_not_written(repo):
    await repo.upsert_listing(listing())
    with pytest.raises(ValueError, match="unknown status"):
        await repo.set_status("p1", "banana")


# -- area cache --------------------------------------------------------------


async def test_area_cache_round_trips_and_misses_cleanly(repo):
    await repo.cache_put("cbs", "BU1", {"kids_pct": 31.5})

    assert await repo.cache_get("cbs", "BU1") == {"kids_pct": 31.5}
    assert await repo.cache_get("cbs", "BU2") is None
    assert await repo.cache_get("politie", "BU1") is None


async def test_expired_cache_entries_are_not_returned(repo):
    await repo.cache_put("cbs", "BU1", {"x": 1}, ttl_days=-1)
    assert await repo.cache_get("cbs", "BU1") is None
