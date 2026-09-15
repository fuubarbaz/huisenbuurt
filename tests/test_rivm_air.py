"""RIVM air-quality provider tests — offline, against a stub WMS.

The behaviour worth pinning is what the provider does with absence. A national
concentration grid has no genuinely-clean holes in it, so a nodata reading is a
bad sample, and reporting it as 0 µg/m³ would score the address perfect on the
strength of a failed request.
"""
from __future__ import annotations

import pytest

from app.models.property import GeoIdentity, ListingSource, PropertyListing
from app.services.enrichment.providers.base import EnrichmentContext, NoDataFound
from app.services.enrichment.providers.rivm_air import AIR_LAYERS, COARSE_LAYERS, RIVMAirQualityProvider
from app.services.scoring import weights as W

# Measured at an Amsterdam address, 2026-09: legal under the EU limits,
# roughly double the WHO 2021 guidelines.
REAL = {"rivm_jaargemiddeld_NO2_actueel": 16.3,
        "rivm_jaargemiddeld_PM25_actueel": 9.2,
        "rivm_jaargemiddeld_PM10_actueel": 17.1}


class FakeWMS:
    """Serves a GRAY_INDEX per layer. A value of None means the call raises."""

    def __init__(self, by_layer): self.by_layer = by_layer; self.asked = []

    async def feature_info(self, layer, **kw):
        self.asked.append(layer)
        value = self.by_layer.get(layer, "absent")
        if isinstance(value, Exception):
            raise value
        if value == "absent":
            return {}
        return {"GRAY_INDEX": value}


def provider(by_layer) -> RIVMAirQualityProvider:
    p = RIVMAirQualityProvider.__new__(RIVMAirQualityProvider)
    p.wms = FakeWMS(by_layer)
    return p


def ctx() -> EnrichmentContext:
    listing = PropertyListing(
        property_id="air-1", source=ListingSource.MANUAL, url="https://e.invalid",
        address="Teststraat 1", postal_code="1015AA", house_number="1",
    )
    geo = GeoIdentity(latitude=52.372, longitude=4.908, rd_x=124400.0, rd_y=486300.0)
    return EnrichmentContext(listing=listing, geo=geo)


# -- the happy path ----------------------------------------------------------


async def test_all_three_pollutants_are_read():
    data = await provider(REAL).fetch(ctx())

    assert data.no2_ug_m3 == pytest.approx(16.3)
    assert data.pm25_ug_m3 == pytest.approx(9.2)
    assert data.pm10_ug_m3 == pytest.approx(17.1)
    assert data.pollutants_mapped == 3


async def test_the_moving_aliases_are_the_layers_requested():
    """Pinning a dated layer would work today and go stale silently."""
    p = provider(REAL)
    await p.fetch(ctx())

    assert set(p.wms.asked) == set(AIR_LAYERS.values())
    assert all("actueel" in layer for layer in p.wms.asked)


async def test_the_computed_count_survives_serialisation():
    """pollutants_mapped is a computed_field; a bare @property would be
    dropped by model_dump and never reach the client."""
    dumped = (await provider(REAL).fetch(ctx())).model_dump()
    assert dumped["pollutants_mapped"] == 3


# -- absence -----------------------------------------------------------------


async def test_the_nodata_sentinel_is_not_read_as_clean_air():
    """-9999 is the raster's nodata marker. Parsed as a concentration it would
    be the cleanest address in the Netherlands."""
    data = await provider({**REAL, "rivm_jaargemiddeld_NO2_actueel": -9999}).fetch(ctx())

    assert data.no2_ug_m3 is None
    assert data.pm25_ug_m3 == pytest.approx(9.2)
    assert data.pollutants_mapped == 2


async def test_a_zero_reading_is_treated_as_nodata():
    """The grid covers the whole country, so nowhere genuinely measures zero."""
    data = await provider({**REAL, "rivm_jaargemiddeld_PM25_actueel": 0.0}).fetch(ctx())
    assert data.pm25_ug_m3 is None


async def test_one_failing_pollutant_does_not_lose_the_others():
    data = await provider(
        {**REAL, "rivm_jaargemiddeld_PM10_actueel": RuntimeError("upstream 502")}
    ).fetch(ctx())

    assert data.pm10_ug_m3 is None
    assert data.no2_ug_m3 == pytest.approx(16.3)


async def test_every_layer_failing_is_reported_as_missing_not_as_clean():
    boom = {layer: RuntimeError("down") for layer in AIR_LAYERS.values()}
    with pytest.raises(NoDataFound, match="no air-quality layer could be sampled"):
        await provider(boom).fetch(ctx())


async def test_a_point_off_the_grid_is_absence_rather_than_a_clean_bill():
    """Every call succeeded and every one was nodata."""
    with pytest.raises(NoDataFound, match="outside the RIVM concentration grid"):
        await provider({layer: -9999 for layer in AIR_LAYERS.values()}).fetch(ctx())


# -- how it scores -----------------------------------------------------------


def test_the_who_guidelines_are_the_anchors_not_the_eu_limits():
    """The EU allows 40 µg/m³ NO2 against WHO's 10. Anchoring on the EU figure
    would mark almost every Dutch address perfect."""
    assert W.WHO_ANNUAL_GUIDELINE_UG_M3["no2"] == 10.0
    assert W.WHO_ANNUAL_GUIDELINE_UG_M3["pm25"] == 5.0


def test_the_environment_shares_still_sum_to_one():
    assert (W.ENVIRONMENT_NOISE_SHARE + W.ENVIRONMENT_AIR_SHARE
            + W.ENVIRONMENT_LIVABILITY_SHARE) == pytest.approx(1.0)


def test_pm25_carries_the_most_weight_of_the_three():
    """It is the largest measured health burden of the pair, so it leads."""
    assert max(W.AIR_POLLUTANT_WEIGHTS, key=W.AIR_POLLUTANT_WEIGHTS.get) == "pm25"
    assert sum(W.AIR_POLLUTANT_WEIGHTS.values()) == pytest.approx(1.0)


# -- coarse mode (national map) ----------------------------------------------


def coarse_provider(by_layer) -> RIVMAirQualityProvider:
    p = RIVMAirQualityProvider.__new__(RIVMAirQualityProvider)
    p.wms = FakeWMS(by_layer)
    p.layers = COARSE_LAYERS
    return p


async def test_coarse_mode_only_requests_pm25():
    p = coarse_provider(REAL)
    await p.fetch(ctx())

    assert p.wms.asked == [AIR_LAYERS["pm25_ug_m3"]]


async def test_coarse_mode_still_reports_pm25_correctly():
    data = await coarse_provider(REAL).fetch(ctx())

    assert data.pm25_ug_m3 == pytest.approx(9.2)
    assert data.no2_ug_m3 is None
    assert data.pm10_ug_m3 is None
    assert data.pollutants_mapped == 1


async def test_coarse_mode_absence_is_still_absence():
    with pytest.raises(NoDataFound):
        await coarse_provider({"rivm_jaargemiddeld_PM25_actueel": -9999}).fetch(ctx())
