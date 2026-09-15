"""Minimal WMS GetFeatureInfo client for point sampling raster layers.

Several Dutch environmental datasets (RIVM noise, Klimaateffectatlas, Bodemloket)
publish rasters rather than queryable tables. The only way to ask "what is the
value at this address" is a GetFeatureInfo request against a tiny bounding box
centred on the point.

Two traps this client exists to avoid:

* **Axis order.** In WMS 1.3.0, ``EPSG:4326`` is latitude-first, the opposite of
  the lon/lat order everything else uses. This client prefers ``EPSG:28992``
  (Rijksdriehoek, unambiguously x/y) and falls back to ``CRS:84``, which is
  explicitly lon/lat. It never emits bare ``EPSG:4326``.
* **Nodata as zero.** Raster layers return ``0`` where a source is not mapped.
  For a decibel layer 0 is not a quiet reading, it means "no contribution
  modelled here" — see :func:`parse_gray_index`.
* **Sampling resolution changes the answer.** GeoServer derives a scale
  denominator from the bounding box and the image size, and that denominator
  decides both how a raster is resampled and — for scale-dependent vector
  layers — *which aggregation level is served at all*. A 3x3 px request over a
  50 m box works out to 1:59,500, which resamples a raster across ~17 m pixels
  and makes Leefbaarometer answer with wijk figures instead of the 100 m grid.
  The defaults below put the request at roughly 1:1,400, fine enough for both.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from app.core.http_client import HttpClient

log = logging.getLogger(__name__)

#: GeoServer returns single-band raster values under this property.
GRAY_INDEX = "GRAY_INDEX"

#: Half-width, in metres, of the box drawn around the point being sampled.
DEFAULT_HALF_EXTENT_M = 50.0

#: Image size the sample is requested at. Together with the extent above this
#: sets the scale denominator; see the module docstring for why it matters.
DEFAULT_PIXELS = 256


class WMSClient:
    def __init__(self, http: HttpClient, base_url: str) -> None:
        self.http = http
        self.base_url = base_url

    @staticmethod
    def scale_denominator(half_extent_m: float, pixels: int) -> float:
        """The OGC scale denominator a request implies, for sanity checks."""
        return (2.0 * half_extent_m / pixels) / 0.00028

    async def feature_info(
        self,
        layer: str,
        *,
        rd_x: Optional[float] = None,
        rd_y: Optional[float] = None,
        lat: Optional[float] = None,
        lon: Optional[float] = None,
        half_extent_m: float = DEFAULT_HALF_EXTENT_M,
        pixels: int = DEFAULT_PIXELS,
    ) -> Optional[dict[str, Any]]:
        """Sample one layer at one point, returning the feature's properties.

        Prefers Rijksdriehoek when the caller has it. ``None`` means the layer
        returned no feature at all, which is distinct from a feature carrying a
        nodata value.
        """
        if rd_x is not None and rd_y is not None:
            crs, x, y = "EPSG:28992", rd_x, rd_y
            half = half_extent_m
        elif lat is not None and lon is not None:
            # CRS:84 is lon/lat, unlike EPSG:4326 under WMS 1.3.0.
            crs, x, y = "CRS:84", lon, lat
            half = half_extent_m / 111_320.0  # metres -> degrees, near enough
        else:
            raise ValueError("feature_info needs either RD or lat/lon coordinates")

        params = {
            "service": "WMS",
            "version": "1.3.0",
            "request": "GetFeatureInfo",
            "layers": layer,
            "query_layers": layer,
            "crs": crs,
            "bbox": f"{x - half},{y - half},{x + half},{y + half}",
            "width": pixels,
            "height": pixels,
            # The centre pixel is the point itself.
            "i": pixels // 2,
            "j": pixels // 2,
            "info_format": "application/json",
            "feature_count": 1,
        }
        payload = await self.http.get_json(
            self.base_url, params=params, headers={"Accept": "application/json"}
        )
        features = payload.get("features") or []
        if not features:
            return None
        return features[0].get("properties") or {}


def parse_gray_index(properties: Optional[dict[str, Any]]) -> Optional[float]:
    """Read a raster sample, mapping the nodata sentinel to ``None``.

    These layers use ``0`` for "not mapped here". Genuine low readings do occur
    (rail Lnight of 14 dB is a real value), so only exact zero is treated as
    absent — a 0 dB noise level is not physically meaningful anyway.
    """
    if not properties:
        return None
    value = properties.get(GRAY_INDEX)
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return None if numeric <= 0 else numeric
