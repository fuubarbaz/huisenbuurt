"""Minimal WFS client for point-in-polygon lookups.

Used for the vector datasets that answer "which zone is this address in" —
foundation-risk areas, flood hazard areas. Preferred over WMS sampling when a
service offers it: the answer is the feature's own attributes rather than a
colour resampled off a raster.

Traps this client exists to avoid:

* **``cql_filter`` is silently ignored** on PDOK's WFS. Passing
  ``cql_filter="pc6='1015AA'"`` returns a 200 with an arbitrary feature from
  somewhere else entirely — verified twice, on the foundation-risk service (it
  answered with a postcode in Vorden) and on BAG (asked for one dwelling in
  Amsterdam, it returned three in Appingedam). Spatial filtering goes through
  ``bbox``, and attribute filtering through the standards-track ``filter``
  parameter — see :meth:`WFSClient.feature_by_id`, which *is* honoured.
* **``bbox`` needs its CRS spelled out.** Without the trailing CRS URI the
  server assumes its own default, which is rarely Rijksdriehoek.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from app.core.http_client import HttpClient

log = logging.getLogger(__name__)

#: Half-width in metres of the query box. A point-sized box can miss a polygon
#: edge; a few metres is enough to land reliably inside the containing zone.
DEFAULT_HALF_EXTENT_M = 15.0

RD_CRS_URI = "urn:ogc:def:crs:EPSG::28992"
WGS84_CRS_URI = "urn:ogc:def:crs:EPSG::4326"


class WFSClient:
    def __init__(self, http: HttpClient, base_url: str) -> None:
        self.http = http
        self.base_url = base_url

    async def features_at(
        self,
        type_name: str,
        *,
        rd_x: Optional[float] = None,
        rd_y: Optional[float] = None,
        lat: Optional[float] = None,
        lon: Optional[float] = None,
        half_extent_m: float = DEFAULT_HALF_EXTENT_M,
        count: int = 10,
    ) -> list[dict[str, Any]]:
        """Return the properties of every feature covering the point."""
        if rd_x is not None and rd_y is not None:
            x, y, crs, half = rd_x, rd_y, RD_CRS_URI, half_extent_m
        elif lat is not None and lon is not None:
            # WFS 2.0 with an EPSG URI is latitude-first for 4326.
            x, y, crs = lat, lon, WGS84_CRS_URI
            half = half_extent_m / 111_320.0
        else:
            raise ValueError("features_at needs either RD or lat/lon coordinates")

        payload = await self.http.get_json(
            self.base_url,
            params={
                "service": "WFS",
                "version": "2.0.0",
                "request": "GetFeature",
                "typeNames": type_name,
                "outputFormat": "application/json",
                "count": count,
                "bbox": f"{x - half},{y - half},{x + half},{y + half},{crs}",
            },
            headers={"Accept": "application/json"},
        )
        return [f.get("properties") or {} for f in (payload.get("features") or [])]

    async def feature_by_id(
        self, type_name: str, *, field: str, value: str
    ) -> Optional[dict[str, Any]]:
        """Fetch one feature by an attribute, using an OGC filter.

        The vendor ``cql_filter`` cannot be used for this — it is accepted and
        ignored, which returns confidently wrong data. The OGC ``filter``
        encoding below is honoured, and the result is checked against the value
        that was asked for regardless.
        """
        ogc_filter = (
            "<Filter xmlns='http://www.opengis.net/ogc'><PropertyIsEqualTo>"
            f"<PropertyName>{field}</PropertyName><Literal>{value}</Literal>"
            "</PropertyIsEqualTo></Filter>"
        )
        payload = await self.http.get_json(
            self.base_url,
            params={
                "service": "WFS", "version": "2.0.0", "request": "GetFeature",
                "typeNames": type_name, "outputFormat": "application/json",
                "count": 1, "filter": ogc_filter,
            },
            headers={"Accept": "application/json"},
        )
        features = payload.get("features") or []
        if not features:
            return None
        properties = features[0].get("properties") or {}
        if str(properties.get(field)) != str(value):
            log.warning(
                "%s filter on %s=%s returned %s instead; discarding",
                type_name, field, value, properties.get(field),
            )
            return None
        return properties
