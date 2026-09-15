"""Leefbaarometer provider tests — offline."""
from __future__ import annotations

import pytest

from app.models.enrichment import ProviderStatus
from app.models.property import GeoIdentity, ListingSource, PropertyListing
from app.services.enrichment.providers.base import EnrichmentContext
from app.services.enrichment.providers.leefbaarometer import (
    DIMENSIONS,
    KLASSE_LABELS,
    LeefbaarometerProvider,
)
from app.services.enrichment.providers.wms import (
    DEFAULT_HALF_EXTENT_M,
    DEFAULT_PIXELS,
    WMSClient,
)

# A real grid response, trimmed: Amsterdam grachtengordel.
GRID_PROPS = {
    "fid": 915, "id": "E1213N4879", "name": "Grachtengordel-West",
    "gemeente": "Amsterdam", "scale": "grid", "year": 2024,
    "lbm": 4.5682, "afw": 0.5226,
    "fys": 0.0011, "onv": -0.0844, "soc": -0.0582, "vrz": 0.6321, "won": 0.0320,
    "kscore": 9, "kafw": 9, "kfys": 5, "konv": 3, "ksoc": 4, "kvrz": 9, "kwon": 5,
}
BUURT_PROPS = {**GRID_PROPS, "id": "BU0363AA06", "scale": "buurt", "kscore": 7}


class FakeWMS:
    def __init__(self, by_layer): self.by_layer, self.calls = by_layer, []

    async def feature_info(self, layer, **kw):
        self.calls.append(layer)
        return self.by_layer.get(layer)


def build(by_layer, edition="24"):
    p = LeefbaarometerProvider.__new__(LeefbaarometerProvider)
    p.http, p.edition, p.wms = None, edition, FakeWMS(by_layer)
    return p


def ctx():
    listing = PropertyListing(
        property_id="l1", source=ListingSource.MANUAL, url="https://e.invalid",
        address="Teststraat 1", postal_code="1015AA", house_number="1",
    )
    return EnrichmentContext(
        listing=listing,
        geo=GeoIdentity(latitude=52.378, longitude=4.893, rd_x=121359.0, rd_y=487957.0),
    )


# -- parsing -----------------------------------------------------------------


async def test_composite_response_is_fully_unpacked():
    p = build({"lbm3:score24_schaalafhankelijk": GRID_PROPS})
    data = await p.fetch(ctx())

    assert data.score == pytest.approx(4.5682)
    assert data.klasse_ordinal == 9
    assert data.klasse == "Uitstekend"
    assert data.aggregation_level == "grid"
    assert data.reference_year == 2024
    assert data.area_name == "Grachtengordel-West"
    assert data.deviation == pytest.approx(0.5226)


async def test_dimensions_are_renamed_from_the_upstream_abbreviations():
    data = await build({"lbm3:score24_schaalafhankelijk": GRID_PROPS}).fetch(ctx())

    assert set(data.dimension_classes) == set(DIMENSIONS.values())
    assert data.dimension_classes["veiligheid"] == 3          # konv
    assert data.dimension_classes["voorzieningen"] == 9       # kvrz
    assert data.dimensions["fysieke_omgeving"] == pytest.approx(0.0011)


def test_class_labels_follow_the_published_legend_order():
    # Counter-intuitive but correct: "Zwak" outranks "Onvoldoende".
    assert KLASSE_LABELS[1] == "Zeer onvoldoende"
    assert KLASSE_LABELS[2] == "Ruim onvoldoende"
    assert KLASSE_LABELS[3] == "Onvoldoende"
    assert KLASSE_LABELS[4] == "Zwak"
    assert KLASSE_LABELS[9] == "Uitstekend"
    assert sorted(KLASSE_LABELS) == list(range(1, 10))


# -- fallback ----------------------------------------------------------------


async def test_falls_back_to_buurt_when_the_grid_has_no_cell():
    p = build({"lbm3:buurtscore24": BUURT_PROPS})   # grid layer returns nothing
    data = await p.fetch(ctx())

    assert data.aggregation_level == "buurt"
    assert p.wms.calls == ["lbm3:score24_schaalafhankelijk", "lbm3:buurtscore24"]


async def test_grid_hit_does_not_query_the_buurt_layer():
    p = build({"lbm3:score24_schaalafhankelijk": GRID_PROPS})
    await p.fetch(ctx())

    assert p.wms.calls == ["lbm3:score24_schaalafhankelijk"]


async def test_no_coverage_anywhere_is_missing():
    result = await build({}).run(ctx(), timeout=5)

    assert result.status is ProviderStatus.MISSING
    assert "no Leefbaarometer" in result.error


async def test_edition_selects_the_layer_names():
    p = build({}, edition="22")
    assert p.grid_layer == "lbm3:score22_schaalafhankelijk"
    assert p.buurt_layer == "lbm3:buurtscore22"


async def test_malformed_values_do_not_raise():
    p = build({"lbm3:score24_schaalafhankelijk": {"lbm": "n/a", "kscore": None, "scale": "grid"}})
    data = await p.fetch(ctx())

    assert data.score is None and data.klasse_ordinal is None and data.klasse is None


# -- sampling resolution -----------------------------------------------------


def test_default_sampling_is_fine_enough_for_the_grid():
    """Above ~1:30,000 the scale-dependent layer serves wijk, not the grid."""
    denom = WMSClient.scale_denominator(DEFAULT_HALF_EXTENT_M, DEFAULT_PIXELS)
    assert denom < 30_000


def test_a_coarse_viewport_would_land_in_the_wijk_range():
    # The old 3px default: valid-looking, but a different aggregation level.
    assert WMSClient.scale_denominator(25.0, 3) > 30_000
