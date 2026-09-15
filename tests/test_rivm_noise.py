"""RIVM noise provider and WMS client tests — offline."""
from __future__ import annotations

import asyncio

import pytest

from app.models.enrichment import ProviderStatus
from app.models.property import GeoIdentity, ListingSource, PropertyListing
from app.services.enrichment.providers.base import EnrichmentContext
from app.services.enrichment.providers.rivm_noise import (
    COARSE_LAYERS,
    NOISE_LAYERS,
    SOURCE_KEYS,
    RIVMNoiseProvider,
)
from app.services.enrichment.providers.wms import GRAY_INDEX, parse_gray_index

ES = RIVMNoiseProvider.energetic_sum


class FakeWMS:
    """Returns scripted GRAY_INDEX values per layer; records the call args."""

    def __init__(self, values: dict[str, object]):
        self.values = values
        self.calls: list[dict] = []

    async def feature_info(self, layer, **kwargs):
        self.calls.append({"layer": layer, **kwargs})
        if layer not in self.values:
            return None
        value = self.values[layer]
        if isinstance(value, Exception):
            raise value
        return {GRAY_INDEX: value}


def build(values) -> RIVMNoiseProvider:
    p = RIVMNoiseProvider.__new__(RIVMNoiseProvider)
    p.http = None
    p.wms = FakeWMS(values)
    return p


def ctx(rd=(121359.0, 487957.0)) -> EnrichmentContext:
    listing = PropertyListing(
        property_id="n1", source=ListingSource.MANUAL, url="https://e.invalid",
        address="Teststraat 1", postal_code="1015AA", house_number="1",
    )
    geo = GeoIdentity(latitude=52.378, longitude=4.893,
                      rd_x=rd[0] if rd else None, rd_y=rd[1] if rd else None)
    return EnrichmentContext(listing=listing, geo=geo)


def by_key(**kw):
    """Map friendly keys to the real layer names the provider requests."""
    return {NOISE_LAYERS[k]: v for k, v in kw.items()}


# -- the nodata sentinel -----------------------------------------------------


def test_zero_is_nodata_not_silence():
    # 0 dB is not a quiet reading; it is "no source mapped here".
    assert parse_gray_index({GRAY_INDEX: 0}) is None
    assert parse_gray_index({GRAY_INDEX: 0.0}) is None
    # Genuine low readings must survive — rail Lnight of 14 dB is real data.
    assert parse_gray_index({GRAY_INDEX: 14}) == 14.0
    assert parse_gray_index({GRAY_INDEX: 72}) == 72.0
    assert parse_gray_index(None) is None
    assert parse_gray_index({}) is None
    assert parse_gray_index({GRAY_INDEX: "n/a"}) is None


async def test_unmapped_sources_are_none_and_counted():
    data = await build(by_key(
        road_lden=55, road_lnight=47, rail_lden=48, rail_lnight=39,
        aviation_lden=0, industry_lden=0, cumulative_lden=56,
    )).fetch(ctx())

    assert data.industry_lden_db is None
    assert data.aviation_lden_db is None
    assert data.sources_mapped == 2          # road + rail only
    assert data.cumulative_lden_db == 56.0


async def test_genuinely_quiet_location_is_distinguishable_from_missing():
    """Nothing mapped anywhere: status is usable, sources_mapped is 0."""
    result = await build(by_key(
        road_lden=0, road_lnight=0, rail_lden=0, rail_lnight=0,
        aviation_lden=0, industry_lden=0, cumulative_lden=0,
    )).run(ctx(), timeout=5)

    assert result.usable
    assert result.payload.sources_mapped == 0
    assert result.payload.cumulative_lden_db is None


# -- resilience --------------------------------------------------------------


async def test_one_failing_layer_does_not_lose_the_others():
    data = await build({
        **by_key(road_lden=55, road_lnight=47, cumulative_lden=56),
        NOISE_LAYERS["rail_lden"]: RuntimeError("layer down"),
    }).fetch(ctx())

    assert data.road_lden_db == 55.0
    assert data.rail_lden_db is None


async def test_every_layer_failing_is_missing_not_quiet():
    """A dead service must never be reported as a silent address."""
    values = {layer: RuntimeError("wms down") for layer in NOISE_LAYERS.values()}
    result = await build(values).run(ctx(), timeout=5)

    assert result.status is ProviderStatus.MISSING
    assert "all noise layers failed" in result.error


async def test_cumulative_falls_back_to_energetic_sum():
    # No allebronnen reading; two 60 dB sources must combine to 63, not 120.
    data = await build(by_key(road_lden=60, rail_lden=60)).fetch(ctx())

    assert data.cumulative_lden_db == pytest.approx(63.0, abs=0.1)


# -- coordinates -------------------------------------------------------------


async def test_rijksdriehoek_is_preferred_when_available():
    provider = build(by_key(road_lden=55))
    await provider.fetch(ctx(rd=(121359.0, 487957.0)))

    call = provider.wms.calls[0]
    assert call["rd_x"] == 121359.0 and call["rd_y"] == 487957.0


async def test_latlon_is_passed_when_rd_is_absent():
    provider = build(by_key(road_lden=55))
    await provider.fetch(ctx(rd=None))

    call = provider.wms.calls[0]
    assert call["rd_x"] is None
    assert call["lat"] == pytest.approx(52.378) and call["lon"] == pytest.approx(4.893)


# -- acoustics ---------------------------------------------------------------


def test_decibels_add_energetically_not_arithmetically():
    assert ES(60.0, 60.0) == pytest.approx(63.0, abs=0.05)   # doubling = +3 dB
    assert ES(60.0, 50.0) == pytest.approx(60.4, abs=0.05)   # quiet source barely counts
    assert ES(70.0) == pytest.approx(70.0)
    assert ES(None, None) is None
    assert ES(55.0, None) == pytest.approx(55.0)


def test_worst_source_ignores_the_cumulative_figure():
    from app.models.enrichment import NoiseData

    noise = NoiseData(road_lden_db=55.0, rail_lden_db=48.0, cumulative_lden_db=56.0)
    assert noise.worst_lden_db == 55.0
    assert NoiseData().worst_lden_db is None


def test_source_keys_are_all_real_layers():
    assert set(SOURCE_KEYS) <= set(NOISE_LAYERS)


# -- coarse mode (national map) ----------------------------------------------


def build_coarse(values) -> RIVMNoiseProvider:
    p = RIVMNoiseProvider.__new__(RIVMNoiseProvider)
    p.http = None
    p.wms = FakeWMS(values)
    p.layers = COARSE_LAYERS
    return p


async def test_coarse_mode_only_requests_the_cumulative_layer():
    p = build_coarse(by_key(cumulative_lden=55.0))
    await p.fetch(ctx())

    requested = {call["layer"] for call in p.wms.calls}
    assert requested == {NOISE_LAYERS["cumulative_lden"]}


async def test_coarse_mode_scores_the_same_as_full_mode_when_cumulative_agrees():
    """The whole reason cumulative_lden can be dropped to alone: real scoring
    already prefers it over summing the sources when both are present."""
    full = build(by_key(cumulative_lden=55.0, road_lden=52.0, rail_lden=40.0))
    coarse = build_coarse(by_key(cumulative_lden=55.0))

    full_data, coarse_data = await asyncio.gather(full.fetch(ctx()), coarse.fetch(ctx()))

    assert full_data.cumulative_lden_db == coarse_data.cumulative_lden_db == 55.0


async def test_coarse_mode_does_not_claim_quiet_when_the_layer_simply_failed():
    """The trap this guards against: sources_mapped defaulting to 0 in coarse
    mode must not be read as 'genuinely quiet' by _environment() the way it
    correctly is in full mode — coarse mode never checked the per-source
    layers at all, so it cannot tell the difference and must not guess."""
    from app.services.enrichment.providers.base import NoDataFound
    p = build_coarse(by_key(cumulative_lden=RuntimeError("layer down")))

    with pytest.raises(NoDataFound):
        await p.fetch(ctx())


async def test_full_mode_with_all_sources_zero_is_still_genuinely_quiet():
    """The control: full mode retains its correct behaviour — this is not a
    regression from adding coarse mode."""
    data = await build(by_key(cumulative_lden=None, road_lden=None, rail_lden=None,
                              aviation_lden=None, industry_lden=None)).fetch(ctx())
    assert data.sources_mapped == 0
    assert data.cumulative_lden_db is None
