"""Leefbaarometer 3.0 — composite livability for the property's location.

Served from the Leefbaarometer's own GeoServer. Note the endpoint: the national
catalogue advertises ``https://geo.leefbaarometer.nl/wms``, which answers 200
with an empty body; the working path is ``/geoserver/wms``.

The ``score{YY}_schaalafhankelijk`` layer is *scale-dependent* — it serves the
100 m grid, buurt, wijk or gemeente aggregation depending on the scale
denominator of the request. A coarse request silently returns a coarser answer
that looks perfectly valid, so the sampling resolution is not incidental here;
see :mod:`app.services.enrichment.providers.wms`.

One GetFeatureInfo call returns the composite score, its 1-9 class, and all
five sub-dimensions with their own classes, so no fan-out is needed.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from app.core.config import settings
from app.core.http_client import HttpClient
from app.models.enrichment import LeefbaarometerData
from app.services.enrichment.providers.base import BaseProvider, EnrichmentContext, NoDataFound
from app.services.enrichment.providers.wms import WMSClient

log = logging.getLogger(__name__)

#: Official class labels, ordinal 1..9, taken from the published legend.
#: Note the order: "Zwak" ranks *above* "Onvoldoende", which is not the
#: sequence a reader would guess from the words alone.
KLASSE_LABELS: dict[int, str] = {
    1: "Zeer onvoldoende",
    2: "Ruim onvoldoende",
    3: "Onvoldoende",
    4: "Zwak",
    5: "Voldoende",
    6: "Ruim voldoende",
    7: "Goed",
    8: "Zeer goed",
    9: "Uitstekend",
}

#: Upstream three-letter dimension keys mapped to readable names.
DIMENSIONS: dict[str, str] = {
    "won": "woningvoorraad",
    "fys": "fysieke_omgeving",
    "vrz": "voorzieningen",
    "soc": "sociale_samenhang",
    "onv": "veiligheid",
}


class LeefbaarometerProvider(BaseProvider[LeefbaarometerData]):
    name = "leefbaarometer"
    source_url = "https://www.leefbaarometer.nl/"

    def __init__(
        self,
        http: HttpClient,
        wms_url: str | None = None,
        edition: str | None = None,
    ) -> None:
        super().__init__(http)
        self.wms = WMSClient(http, wms_url or settings.leefbaarometer_wms)
        self.edition = edition or settings.leefbaarometer_edition

    @property
    def grid_layer(self) -> str:
        return f"lbm3:score{self.edition}_schaalafhankelijk"

    @property
    def buurt_layer(self) -> str:
        return f"lbm3:buurtscore{self.edition}"

    async def fetch(self, ctx: EnrichmentContext) -> LeefbaarometerData:
        lat, lon = ctx.lat_lon

        async def sample(layer: str) -> Optional[dict[str, Any]]:
            return await self.wms.feature_info(
                layer, rd_x=ctx.geo.rd_x, rd_y=ctx.geo.rd_y, lat=lat, lon=lon
            )

        # The grid does not cover every square metre (water, industry, verges),
        # so fall back to the buurt polygon, which does.
        props = await sample(self.grid_layer)
        if not props:
            log.debug("no Leefbaarometer grid cell at %s,%s; trying buurt", lat, lon)
            props = await sample(self.buurt_layer)
        if not props:
            raise NoDataFound("no Leefbaarometer cell or buurt at this location")

        return self._to_model(props)

    @staticmethod
    def _to_model(props: dict[str, Any]) -> LeefbaarometerData:
        ordinal = _as_int(props.get("kscore"))

        dimensions: dict[str, float] = {}
        dimension_classes: dict[str, int] = {}
        for key, name in DIMENSIONS.items():
            value = _as_float(props.get(key))
            if value is not None:
                dimensions[name] = round(value, 4)
            klass = _as_int(props.get(f"k{key}"))
            if klass is not None:
                dimension_classes[name] = klass

        return LeefbaarometerData(
            score=_as_float(props.get("lbm")),
            klasse=KLASSE_LABELS.get(ordinal) if ordinal else None,
            klasse_ordinal=ordinal,
            dimensions=dimensions,
            dimension_classes=dimension_classes,
            deviation=_as_float(props.get("afw")),
            aggregation_level=props.get("scale"),
            area_name=props.get("name"),
            reference_year=_as_int(props.get("year")),
        )


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
