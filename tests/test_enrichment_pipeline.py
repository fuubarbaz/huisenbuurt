"""Pipeline contract tests — no network required.

These pin the two behaviours the whole design rests on: a failing provider
degrades instead of aborting, and the bundle shape is always complete.
"""
from __future__ import annotations

import asyncio

import pytest

from app.models.enrichment import DemographicsData, EnrichmentBundle, ProviderStatus
from app.models.property import GeoIdentity, ListingSource, PropertyListing
from app.services.enrichment.enrichment_pipeline import EnrichmentPipeline
from app.services.enrichment.providers.base import BaseProvider, NoDataFound


@pytest.fixture
def listing() -> PropertyListing:
    return PropertyListing(
        property_id="test-1",
        source=ListingSource.MANUAL,
        url="https://example.invalid/1",
        address="Teststraat 1, Amsterdam",
        postal_code="1015 AA",
        house_number="1",
        price_eur=550_000,
        living_area_m2=100,
        construction_year=1962,
    )


@pytest.fixture
def geo() -> GeoIdentity:
    return GeoIdentity(latitude=52.37, longitude=4.89, buurtcode="BU03630001", gemeentecode="GM0363")


class OkProvider(BaseProvider):
    name = "ok"

    async def fetch(self, ctx):
        return DemographicsData(households_with_children_pct=31.0)


class BoomProvider(BaseProvider):
    name = "boom"

    async def fetch(self, ctx):
        raise RuntimeError("upstream on fire")


class SlowProvider(BaseProvider):
    name = "slow"

    async def fetch(self, ctx):
        await asyncio.sleep(5)
        return DemographicsData()


class EmptyProvider(BaseProvider):
    name = "empty"

    async def fetch(self, ctx):
        raise NoDataFound("nothing here")


def _pipeline(providers: dict) -> EnrichmentPipeline:
    return EnrichmentPipeline(http=None, geocoder=object(), providers=providers, provider_timeout=0.2)


async def test_one_failing_provider_does_not_abort_the_run(listing, geo):
    pipeline = _pipeline({"demographics": OkProvider(None), "crime": BoomProvider(None)})
    bundle = await pipeline.enrich_one(listing, geo=geo)

    # OkProvider fills one field of many, so PARTIAL is the correct verdict.
    assert bundle.demographics.status is ProviderStatus.PARTIAL
    assert bundle.demographics.usable
    assert bundle.crime.status is ProviderStatus.ERROR
    assert not bundle.crime.usable
    assert "upstream on fire" in bundle.crime.error


async def test_slow_provider_is_cut_off_at_the_timeout(listing, geo):
    pipeline = _pipeline({"noise": SlowProvider(None)})
    bundle = await pipeline.enrich_one(listing, geo=geo)

    assert bundle.noise.status is ProviderStatus.ERROR
    assert "timeout" in bundle.noise.error


async def test_no_data_is_missing_not_error(listing, geo):
    pipeline = _pipeline({"soil": EmptyProvider(None)})
    bundle = await pipeline.enrich_one(listing, geo=geo)

    assert bundle.soil.status is ProviderStatus.MISSING


async def test_unrun_layers_are_still_present_in_the_bundle(listing, geo):
    pipeline = _pipeline({"demographics": OkProvider(None)})
    bundle = await pipeline.enrich_one(listing, geo=geo)

    # Every layer exists; the five that had no provider are SKIPPED.
    assert bundle.education.status is ProviderStatus.SKIPPED
    assert bundle.coverage_pct == pytest.approx(100 / len(EnrichmentBundle.LAYER_NAMES), abs=0.2)


async def test_postal_code_is_normalised(listing):
    assert listing.postal_code == "1015AA"
    assert listing.pc4 == "1015"
    assert listing.is_pre_1970 is True


# -- what reaches a client ---------------------------------------------------


async def test_usable_and_coverage_survive_serialisation(listing, geo):
    """A client decides whether to render a layer from `usable`, and a plain
    @property is dropped by model_dump — the trap that made WozTrend's
    percentages arrive as absent."""
    pipeline = _pipeline({"demographics": OkProvider(None), "crime": BoomProvider(None)})
    dumped = (await pipeline.enrich_one(listing, geo=geo)).model_dump()

    assert dumped["demographics"]["usable"] is True
    assert dumped["crime"]["usable"] is False
    assert dumped["coverage_pct"] == pytest.approx(100 / len(EnrichmentBundle.LAYER_NAMES), abs=0.2)
