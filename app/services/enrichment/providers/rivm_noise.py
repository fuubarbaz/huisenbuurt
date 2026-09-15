"""RIVM noise maps (geluidbelastingkaarten), via Atlas Leefomgeving WMS.

Each source — road, rail, aviation, industry — is a separate raster in Lden
(day-evening-night) and, for the two ground-traffic sources, Lnight. Every
layer is sampled at the property's coordinate with one GetFeatureInfo call;
the seven calls are independent and run concurrently.

Two things worth knowing about the data:

* **``_actueel`` aliases.** RIVM keeps dated layer names (``..._2022``) and
  publishes moving aliases that always resolve to the newest edition. The
  aliases are used wherever they exist so the provider does not silently go
  stale; aviation has no alias and is pinned, with the year in the name.
* **Zero means "not mapped", not "silent".** Away from any industrial estate
  the industry layer reads 0. That is the absence of a source, so it is parsed
  to ``None`` and simply contributes nothing.

RIVM also publishes its own cumulative layer (``allebronnen``), which is
preferred over summing the sources here — it is computed from the underlying
models rather than from rounded per-source rasters.
"""
from __future__ import annotations

import asyncio
import logging
import math
from typing import Optional

from app.core.config import settings
from app.core.http_client import HttpClient
from app.models.enrichment import NoiseData
from app.services.enrichment.providers.base import BaseProvider, EnrichmentContext, NoDataFound
from app.services.enrichment.providers.wms import WMSClient, parse_gray_index

log = logging.getLogger(__name__)

#: WMS layer per measurement. Names are case-sensitive and inconsistently
#: capitalised upstream, so they are reproduced exactly as published.
NOISE_LAYERS: dict[str, str] = {
    "road_lden": "rivm_Geluid_lden_wegverkeer_actueel",
    "road_lnight": "rivm_Geluid_lnight_wegverkeer_actueel",
    "rail_lden": "rivm_Geluid_lden_treinverkeer_actueel",
    "rail_lnight": "rivm_Geluid_lnight_treinverkeer_actueel",
    "industry_lden": "rivm_Geluid_lden_industrie_actueel",
    # No moving alias published for aviation; pinned to the newest edition.
    "aviation_lden": "rivm_20241201_Geluid_lden_vliegverkeer_2022",
    # RIVM's own cumulative computation across all sources.
    "cumulative_lden": "rivm_Geluid_lden_allebronnen_actueel",
}

#: Which readings count as "a source was mapped here" for coverage purposes.
SOURCE_KEYS = ("road_lden", "rail_lden", "aviation_lden", "industry_lden")


#: The single call a national-overview map needs (see build_national_map.py):
#: RIVM's own cumulative layer is already what real address-scoring PREFERS
#: for the score itself — "preferred over summing the sources here", below —
#: so this drops zero scoring fidelity for the map. What it gives up is the
#: per-source WHO-guideline flags, which a buurt-level overview never shows.
COARSE_LAYERS: dict[str, str] = {"cumulative_lden": NOISE_LAYERS["cumulative_lden"]}


class RIVMNoiseProvider(BaseProvider[NoiseData]):
    name = "rivm_noise"
    source_url = "https://www.atlasleefomgeving.nl/"
    #: Class-level default so tests built via __new__ (bypassing __init__)
    #: still work — the same trap this codebase has hit before with other
    #: instance attributes assigned only in __init__.
    layers: dict[str, str] = NOISE_LAYERS

    def __init__(
        self, http: HttpClient, wms_url: str | None = None,
        layers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(http)
        self.wms = WMSClient(http, wms_url or settings.rivm_noise_wms)
        #: Overridable so a caller can request fewer layers (see COARSE_LAYERS
        #: above); a real address score always gets the full set by default.
        self.layers = layers if layers is not None else NOISE_LAYERS

    async def fetch(self, ctx: EnrichmentContext) -> NoiseData:
        lat, lon = ctx.lat_lon

        async def sample(layer: str) -> Optional[float]:
            props = await self.wms.feature_info(
                layer, rd_x=ctx.geo.rd_x, rd_y=ctx.geo.rd_y, lat=lat, lon=lon
            )
            return parse_gray_index(props)

        keys = list(self.layers)
        settled = await asyncio.gather(
            *(sample(self.layers[k]) for k in keys), return_exceptions=True
        )

        readings: dict[str, Optional[float]] = {}
        failures = 0
        for key, outcome in zip(keys, settled):
            if isinstance(outcome, BaseException):
                log.warning("noise layer %s failed: %s", key, outcome)
                readings[key] = None
                failures += 1
            else:
                readings[key] = outcome

        # Every layer erroring means the service is down, not that it is quiet.
        if failures == len(keys):
            raise NoDataFound("all noise layers failed")

        # Whether the four per-source layers were even asked for. Only then
        # does "none of them mapped anything" honestly mean "genuinely quiet"
        # rather than "we did not check" — the trap a coarse caller (see
        # COARSE_LAYERS) would otherwise fall into: cumulative_lden failing
        # with sources_mapped defaulting to 0 must NOT be read as silence.
        sources_attempted = set(SOURCE_KEYS).issubset(self.layers)

        cumulative = readings.get("cumulative_lden")
        if cumulative is None and sources_attempted:
            # Fall back to summing the sources energetically.
            cumulative = self.energetic_sum(*(readings.get(k) for k in SOURCE_KEYS))
        if cumulative is None and not sources_attempted:
            raise NoDataFound("cumulative noise layer failed and no per-source fallback was requested")

        return NoiseData(
            road_lden_db=readings.get("road_lden"),
            road_lnight_db=readings.get("road_lnight"),
            rail_lden_db=readings.get("rail_lden"),
            rail_lnight_db=readings.get("rail_lnight"),
            aviation_lden_db=readings.get("aviation_lden"),
            industry_lden_db=readings.get("industry_lden"),
            cumulative_lden_db=cumulative,
            sources_mapped=(sum(1 for k in SOURCE_KEYS if readings.get(k) is not None)
                           if sources_attempted else 0),
        )

    @staticmethod
    def energetic_sum(*levels: Optional[float]) -> Optional[float]:
        """Combine dB levels the way sound actually adds, not arithmetically."""
        present = [lv for lv in levels if lv is not None]
        if not present:
            return None
        return round(10.0 * math.log10(sum(10 ** (lv / 10.0) for lv in present)), 1)
