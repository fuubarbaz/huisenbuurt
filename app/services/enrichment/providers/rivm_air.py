"""RIVM air quality — annual-mean NO2, PM2.5 and PM10, via Atlas Leefomgeving.

The same WMS server and the same GetFeatureInfo sampling as the noise maps
next door, so this provider is deliberately thin: three concurrent point
samples and a WHO comparison.

Why it belongs in the environmental dimension
---------------------------------------------
Noise and air pollution are the two measured environmental exposures at a Dutch
address, and PM2.5 is the larger health burden of the pair. Scoring noise alone
would call a quiet street beside a motorway healthy.

Two decisions worth stating:

* **Moving aliases, not pinned years.** ``rivm_jaargemiddeld_NO2_actueel`` and
  its two siblings always resolve to the newest published grid, the same
  convention the noise provider relies on. Pinning ``..._2019`` would work
  today and quietly go stale.
* **WHO 2021, not the EU limit values.** The EU annual limit for NO2 is
  40 µg/m³ against WHO's 10, and for PM2.5 it is 25 against WHO's 5. Measured
  at an Amsterdam address this layer reads NO2 16.3 and PM2.5 9.2 — clean under
  the EU numbers, roughly double the WHO guideline under the real ones. Scoring
  on the EU figure would mark almost every Dutch address perfect and tell a
  buyer nothing, so the WHO guidelines are the anchors.

These are modelled background concentrations on a national grid, not a kerbside
measurement: a flat on a busy canal reads close to its neighbourhood rather
than to the traffic lane outside its window.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from app.core.config import settings
from app.core.http_client import HttpClient
from app.models.enrichment import AirQualityData
from app.services.enrichment.providers.base import BaseProvider, EnrichmentContext, NoDataFound
from app.services.enrichment.providers.wms import WMSClient, parse_gray_index

log = logging.getLogger(__name__)

#: Moving aliases: RIVM repoints these at each new edition.
AIR_LAYERS: dict[str, str] = {
    "no2_ug_m3": "rivm_jaargemiddeld_NO2_actueel",
    "pm25_ug_m3": "rivm_jaargemiddeld_PM25_actueel",
    "pm10_ug_m3": "rivm_jaargemiddeld_PM10_actueel",
}

#: A concentration grid covers the whole country, so a reading of exactly zero
#: is a nodata artefact rather than perfectly clean air. parse_gray_index
#: already maps the -9999 sentinel to None; this catches the other form.
MIN_PLAUSIBLE_UG_M3 = 0.01


#: The single call a national-overview map needs (see build_national_map.py).
#: Unlike noise's COARSE_LAYERS, this is a real fidelity tradeoff, not a free
#: one: real scoring blends NO2 (35%), PM2.5 (50%) and PM10 (15%), and this
#: keeps only PM2.5. Chosen because it carries the largest weight of the
#: three and the largest measured health burden — the best single proxy
#: available, not a lossless reduction the way dropping noise's per-source
#: layers is.
COARSE_LAYERS: dict[str, str] = {"pm25_ug_m3": AIR_LAYERS["pm25_ug_m3"]}


class RIVMAirQualityProvider(BaseProvider[AirQualityData]):
    name = "rivm_air"
    source_url = "https://www.atlasleefomgeving.nl/"
    #: Class-level default so tests built via __new__ (bypassing __init__)
    #: still work — the same trap this codebase has hit before with other
    #: instance attributes assigned only in __init__.
    layers: dict[str, str] = AIR_LAYERS

    def __init__(
        self, http: HttpClient, wms_url: str | None = None,
        layers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(http)
        self.wms = WMSClient(http, wms_url or settings.rivm_air_wms)
        #: Overridable so a caller can request fewer pollutants (see
        #: COARSE_LAYERS above); a real address score always gets all three.
        self.layers = layers if layers is not None else AIR_LAYERS

    async def fetch(self, ctx: EnrichmentContext) -> AirQualityData:
        lat, lon = ctx.lat_lon

        async def sample(layer: str) -> Optional[float]:
            props = await self.wms.feature_info(
                layer, rd_x=ctx.geo.rd_x, rd_y=ctx.geo.rd_y, lat=lat, lon=lon
            )
            value = parse_gray_index(props)
            if value is None or value < MIN_PLAUSIBLE_UG_M3:
                return None
            return round(value, 1)

        keys = list(self.layers)
        settled = await asyncio.gather(
            *(sample(self.layers[k]) for k in keys), return_exceptions=True
        )

        readings: dict[str, Optional[float]] = {}
        failures = 0
        for key, outcome in zip(keys, settled):
            if isinstance(outcome, BaseException):
                # One pollutant failing is not the layer failing; note it and
                # let the other two answer.
                log.warning("air quality: %s failed: %s", self.layers[key], outcome)
                failures += 1
                readings[key] = None
            else:
                readings[key] = outcome

        if failures == len(keys):
            raise NoDataFound("no air-quality layer could be sampled at this point")
        if not any(v is not None for v in readings.values()):
            # Every call succeeded and every one was nodata. Off the grid — the
            # honest answer is absence, not a clean bill of health.
            raise NoDataFound("point falls outside the RIVM concentration grid")

        return AirQualityData(**readings, reference="RIVM jaargemiddelde (actueel)")
