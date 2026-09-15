"""Door-to-door travel time from a property to places that matter to you.

Every other layer answers "is this a good area". This one answers "can I
actually live here", which for most buyers decides the shortlist.

Routers, and why these ones
---------------------------
* **Car — OSRM** (``router.project-osrm.org``), keyless.
* **Bike — BRouter** (``brouter.de``), keyless, with a cycling profile.

The obvious move is to use OSRM for both, and it is wrong. Its public demo
server carries only the driving graph and answers *every* profile from it:
asking for ``cycling``, ``walking`` or ``foot`` between the same two points
returns byte-identical duration and distance to ``driving``. Verified — 8.8 min
and 11.06 km for all five. A bike time from OSRM would be a car time with a
different label, so bikes go to BRouter, which routes the same journey as
5.2 km in 15 min: shorter and more direct, which is what a bike actually does
in a Dutch city.

* **Public transport — needs an API key.** No keyless door-to-door transit
  router exists for the Netherlands: NS, transit.land and OpenRouteService all
  return 401, and the OTP instances are gone. Set ``WOONAGENT_ORS_API_KEY``
  (free from openrouteservice.org) to enable it; without one the transit column
  is simply absent rather than guessed at.

Both public routers are shared community services. They are called once per
destination per property and go through the same rate limiter as everything
else; if you scale this up, host your own.
"""
from __future__ import annotations

import asyncio
import logging
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.http_client import HttpClient
from app.models.property import GeoIdentity
from app.services.geo.pdok_locatieserver import GeocodeError, PDOKLocatieserver

log = logging.getLogger(__name__)

OSRM_URL = "https://router.project-osrm.org/route/v1"
BROUTER_URL = "https://brouter.de/brouter"
ORS_URL = "https://api.openrouteservice.org/v2/directions"

#: BRouter profile. "trekking" is its everyday-cycling profile; "fastbike"
#: assumes a road bike and flatters the time.
BROUTER_PROFILE = "trekking"

MAX_DESTINATIONS = 6


class TravelMode(str, Enum):
    CAR = "car"
    BIKE = "bike"
    TRANSIT = "transit"


class CommuteLeg(BaseModel):
    mode: TravelMode
    duration_min: float
    distance_km: float
    router: str = Field(..., description="Which service produced this, for provenance.")


class CommuteToDestination(BaseModel):
    label: str
    query: str
    matched_address: Optional[str] = Field(
        None, description="What the destination resolved to — shown so a bad guess is visible."
    )
    legs: list[CommuteLeg] = Field(default_factory=list)
    error: Optional[str] = None

    def leg(self, mode: TravelMode) -> Optional[CommuteLeg]:
        return next((leg for leg in self.legs if leg.mode is mode), None)


class CommuteResult(BaseModel):
    destinations: list[CommuteToDestination] = Field(default_factory=list)
    transit_available: bool = Field(
        False, description="False when no routing key is configured for public transport."
    )


class CommuteService:
    def __init__(self, http: HttpClient) -> None:
        self.http = http
        self.geocoder = PDOKLocatieserver(http)

    async def travel_times(
        self, origin: GeoIdentity, destinations: list[dict]
    ) -> CommuteResult:
        """Times from one property to several places, all modes in parallel."""
        wanted = destinations[:MAX_DESTINATIONS]
        results = await asyncio.gather(
            *(self._one(origin, d) for d in wanted), return_exceptions=True
        )

        out: list[CommuteToDestination] = []
        for spec, outcome in zip(wanted, results):
            if isinstance(outcome, BaseException):
                log.warning("commute to %s failed: %s", spec.get("query"), outcome)
                out.append(CommuteToDestination(
                    label=spec.get("label") or spec.get("query", ""),
                    query=spec.get("query", ""), error=str(outcome)))
            else:
                out.append(outcome)

        return CommuteResult(
            destinations=out, transit_available=bool(settings.ors_api_key)
        )

    async def _one(self, origin: GeoIdentity, spec: dict) -> CommuteToDestination:
        query = (spec.get("query") or "").strip()
        label = (spec.get("label") or query).strip()
        if not query:
            return CommuteToDestination(label=label, query=query, error="no destination given")

        try:
            target = await self.geocoder.search(query)
        except GeocodeError as exc:
            return CommuteToDestination(label=label, query=query, error=str(exc))
        if target is None:
            return CommuteToDestination(
                label=label, query=query, error=f"could not find {query!r}")

        legs = await asyncio.gather(
            self._car(origin, target), self._bike(origin, target),
            self._transit(origin, target), return_exceptions=True,
        )
        return CommuteToDestination(
            label=label, query=query, matched_address=target.matched_address,
            legs=[leg for leg in legs if isinstance(leg, CommuteLeg)],
        )

    # -- per mode ----------------------------------------------------------

    async def _car(self, a: GeoIdentity, b: GeoIdentity) -> Optional[CommuteLeg]:
        try:
            payload = await self.http.get_json(
                f"{OSRM_URL}/driving/{a.longitude},{a.latitude};{b.longitude},{b.latitude}",
                params={"overview": "false"},
            )
        except Exception as exc:  # noqa: BLE001 — one mode failing is not fatal
            log.warning("OSRM car routing failed: %s", exc)
            return None
        routes = payload.get("routes") or []
        if not routes:
            return None
        return CommuteLeg(
            mode=TravelMode.CAR,
            duration_min=round(routes[0]["duration"] / 60, 1),
            distance_km=round(routes[0]["distance"] / 1000, 1),
            router="OSRM",
        )

    async def _bike(self, a: GeoIdentity, b: GeoIdentity) -> Optional[CommuteLeg]:
        """BRouter, not OSRM — see the module docstring for why."""
        try:
            payload = await self.http.get_json(
                BROUTER_URL,
                params={
                    "lonlats": f"{a.longitude},{a.latitude}|{b.longitude},{b.latitude}",
                    "profile": BROUTER_PROFILE, "alternativeidx": 0, "format": "geojson",
                },
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("BRouter cycling routing failed: %s", exc)
            return None
        features = payload.get("features") or []
        if not features:
            return None
        props = features[0].get("properties") or {}
        try:
            seconds = float(props["total-time"])
            metres = float(props["track-length"])
        except (KeyError, TypeError, ValueError):
            return None
        return CommuteLeg(
            mode=TravelMode.BIKE,
            duration_min=round(seconds / 60, 1),
            distance_km=round(metres / 1000, 1),
            router="BRouter",
        )

    async def _transit(self, a: GeoIdentity, b: GeoIdentity) -> Optional[CommuteLeg]:
        """Public transport. Absent unless a routing key is configured."""
        key = settings.ors_api_key
        if not key:
            return None
        try:
            payload = await self.http.get_json(
                f"{ORS_URL}/public-transport",
                params={"start": f"{a.longitude},{a.latitude}",
                        "end": f"{b.longitude},{b.latitude}"},
                headers={"Authorization": key},
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("public-transport routing failed: %s", exc)
            return None
        features = payload.get("features") or []
        if not features:
            return None
        summary = ((features[0].get("properties") or {}).get("summary") or {})
        if not summary.get("duration"):
            return None
        return CommuteLeg(
            mode=TravelMode.TRANSIT,
            duration_min=round(summary["duration"] / 60, 1),
            distance_km=round(summary.get("distance", 0) / 1000, 1),
            router="OpenRouteService",
        )
