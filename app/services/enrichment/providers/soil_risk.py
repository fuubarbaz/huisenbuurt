"""Foundation, soil and flood risk — the structural-security layer.

Primary source is RVO's "Indicatieve aandachtsgebieden funderingsproblematiek",
published on PDOK as a PC6-keyed polygon layer. It classifies each postcode by
soil vulnerability and by how much of the local building stock predates 1970 —
the era before deep concrete piles became standard, when houses in the west sat
on wooden piles that rot once the water table drops (*paalrot*). Repair runs to
six figures, so this is the single most consequential layer in the engine.

The provider deliberately combines the area classification with **this
property's own construction year** rather than the area's pre-1970 share. Both
halves are needed for real exposure: vulnerable ground under a 2005 house is a
non-issue, and a 1920 house on sand is a much smaller one. Where the listing
has no construction year the area share stands in, at reduced confidence.

Gaps, stated plainly rather than papered over
---------------------------------------------
* **Subsidence rate (mm/yr).** No national open service was reachable; the
  Bodemdalingskaart portal and the PDOK path both 404. ``subsidence_mm_per_year``
  stays ``None`` until one is wired.
* **Contamination (Bodemloket).** Every published host for the Bodemloket map
  service is dead or 404s. ``contamination_status`` stays ``None``.
* **Flood depth.** RIVM's own flood-probability layer (below) has no depth
  dimension — it is a return-period class, not a water-level model — so
  ``flood_depth_m`` stays ``None`` until a depth source is wired.

Flood probability, and why the INSPIRE layer this used to read was dropped
-------------------------------------------------------------------------
The EU Floods Directive "hazard area" layer this provider used to read was a
bare yes/no with no return period, and it did not hold up: Apeldoorn (sandy,
inland, high ground) came back inside the hazard area while Amsterdam (below
sea level) came back outside it. It was surfaced as an unscored flag rather
than trusted with a number.

RIVM's own ``kans_overstroming`` layer, on the same Atlas Leefomgeving WMS the
noise and air providers already sample, is a proper return-period
classification — GetLegendGraphic on the live service names six categories:
"Overstroomt niet" (1) through "1x per 10 jaar" (5), plus "Oppervlaktewater"
(6, the sample point landed on open water) and a nodata sentinel. Sampled and
compared against places with known flood histories before trusting it:
Dordrecht (historically flood-prone, an island city) reads 2; a Zuid-Holland
polder well below sea level reads 4; well-defended Amsterdam and Nijmegen both
read 1 — consistent with *residual* risk behind the current flood defences,
which is the right thing to score, rather than raw elevation.

The model fields are kept so a future source drops in without a schema change.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from app.core.config import settings
from app.core.http_client import HttpClient
from app.models.enrichment import SoilRiskData
from app.services.enrichment.providers.base import BaseProvider, EnrichmentContext, NoDataFound
from app.services.enrichment.providers.wfs import WFSClient
from app.services.enrichment.providers.wms import WMSClient, parse_gray_index

log = logging.getLogger(__name__)

FOUNDATION_TYPE_NAME = "indgebfunderingsproblematiek:indgebfunderingsproblematiek"

#: RIVM's national flood-probability grid, on the same Atlas Leefomgeving WMS
#: the noise and air providers already sample.
FLOOD_LAYER = "20231201_kans_overstroming"

#: RIVM's own labels, read from the live service's GetLegendGraphic response
#: rather than guessed — see the module docstring for the verification.
FLOOD_LABELS: dict[int, str] = {
    1: "overstroomt niet", 2: "1x per 100.000 jaar", 3: "1x per 1.000 jaar",
    4: "1x per 100 jaar", 5: "1x per 10 jaar",
}

#: Vulnerability classes in the ``legenda`` string, sampled nationwide.
#: "Overig" covers roughly 4% of postcodes and states no verdict either way,
#: so those are judged on the soil region alone rather than assumed safe.
VULNERABILITY_CLASSES = {
    "niet kwetsbaar gebied", "stedelijk gebied", "kwetsbaar gebied", "overig",
}
UNCLASSIFIED_VULNERABILITY = "overig"

#: Physisch-geografische regio, sampled nationwide: Niet indeelbaar,
#: Zeekleigebied, Hogere Zandgronden, Heuvelland, Rivierengebied,
#: Laagveengebied, Afgesloten Zeearmen, Getijdengebied.
#: Soft, wet ground is where shallow and timber foundations get into trouble;
#: sand and limestone are not a concern.
HIGH_RISK_SOIL_REGIONS = {
    "laagveengebied", "zeekleigebied", "getijdengebied", "afgesloten zeearmen",
}
PEAT_REGIONS = {"laagveengebied"}

#: Urban postcodes come back as "Stedelijk gebied" with fgr "Niet indeelbaar",
#: because city ground is too disturbed to classify. The dataset's own guidance
#: fills the gap: "in general, cities in West and North Netherlands have
#: vulnerable soil areas — attention to wooden pile foundations is warranted
#: there". Province therefore stands in for the missing soil class in cities.
VULNERABLE_URBAN_PROVINCES = {
    "noord-holland", "zuid-holland", "utrecht", "zeeland",
    "friesland", "fryslân", "fryslan", "groningen", "flevoland",
}

#: Ordinal -> label, matching the vocabulary used elsewhere in the engine.
RISK_LABELS: dict[int, str] = {
    0: "geen", 1: "laag", 2: "matig", 3: "hoog", 4: "zeer hoog",
}

#: Pre-1970 construction is the era of shallow and wooden foundations.
FOUNDATION_ERA_CUTOFF = 1970


class SoilRiskProvider(BaseProvider[SoilRiskData]):
    name = "soil_risk"
    source_url = "https://www.kcaf.nl/"

    def __init__(
        self,
        http: HttpClient,
        foundation_url: str | None = None,
        flood_wms_url: str | None = None,
    ) -> None:
        super().__init__(http)
        self.foundation = WFSClient(http, foundation_url or settings.funderingsrisico_wfs)
        self.flood = WMSClient(http, flood_wms_url or settings.rivm_flood_wms)

    async def fetch(self, ctx: EnrichmentContext) -> SoilRiskData:
        coords = {
            "rd_x": ctx.geo.rd_x, "rd_y": ctx.geo.rd_y,
            "lat": ctx.lat_lon[0], "lon": ctx.lat_lon[1],
        }

        foundation_rows, flood_raw = await asyncio.gather(
            self.foundation.features_at(FOUNDATION_TYPE_NAME, **coords),
            self._sample_flood(ctx),
            return_exceptions=True,
        )

        if isinstance(foundation_rows, BaseException):
            raise NoDataFound(f"foundation layer unavailable: {foundation_rows}")
        if not foundation_rows:
            raise NoDataFound("no foundation-risk polygon covers this address")

        row = self._best_match(foundation_rows, ctx.listing.postal_code)

        flood_ordinal: Optional[int] = None
        if isinstance(flood_raw, BaseException):
            log.warning("flood layer failed: %s", flood_raw)
        else:
            flood_ordinal = flood_raw

        return self._to_model(row, ctx, flood_ordinal=flood_ordinal)

    async def _sample_flood(self, ctx: EnrichmentContext) -> Optional[int]:
        props = await self.flood.feature_info(
            FLOOD_LAYER, rd_x=ctx.geo.rd_x, rd_y=ctx.geo.rd_y,
            lat=ctx.lat_lon[0], lon=ctx.lat_lon[1],
        )
        value = parse_gray_index(props)
        if value is None:
            return None
        ordinal = int(value)
        # 6 = "Oppervlaktewater": the sample point landed on open water, not a
        # building site. That is a point-sampling artefact near canal-side
        # addresses, not a flood-probability class — report absence rather
        # than a nonsensical "worse than 1-in-10-years" reading.
        return ordinal if 1 <= ordinal <= 5 else None

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _best_match(rows: list[dict[str, Any]], postal_code: str) -> dict[str, Any]:
        """Prefer the polygon whose PC6 matches the listing's own postcode.

        The query box can clip a neighbouring postcode; the listing's own PC6 is
        the tie-breaker when it is present.
        """
        for row in rows:
            if str(row.get("pc6", "")).strip().upper() == postal_code.upper():
                return row
        return rows[0]

    @classmethod
    def _to_model(
        cls,
        row: dict[str, Any],
        ctx: EnrichmentContext,
        *,
        flood_ordinal: Optional[int],
    ) -> SoilRiskData:
        vulnerability, _era_band = cls._split_legenda(row.get("legenda"))
        soil_region = (row.get("fgr") or "").strip() or None
        province = (row.get("provincie") or "").strip() or None

        year = ctx.listing.construction_year
        area_pre1970_pct = _as_float(row.get("percvoor1970"))
        # The house's own age when known; the area's share as a stand-in.
        pre_1970 = (
            year < FOUNDATION_ERA_CUTOFF if year is not None
            else (area_pre1970_pct or 0) >= 50.0
        )

        ordinal = cls._risk_ordinal(vulnerability, soil_region, province, pre_1970)

        return SoilRiskData(
            foundation_risk_class=RISK_LABELS.get(ordinal) if ordinal is not None else None,
            foundation_risk_ordinal=ordinal,
            paalrot_risk=cls._paalrot(vulnerability, soil_region, province, pre_1970),
            soil_type=soil_region,
            soil_vulnerability=vulnerability,
            area_pre_1970_pct=area_pre1970_pct,
            area_buildings=_as_int(row.get("nBag")),
            area_postcode=str(row.get("pc6") or "").strip() or None,
            construction_year_known=year is not None,
            flood_risk_ordinal=flood_ordinal,
            flood_risk_class=FLOOD_LABELS.get(flood_ordinal) if flood_ordinal else None,
            # Explicitly unavailable — see the module docstring.
            subsidence_mm_per_year=None,
            flood_depth_m=None,
            contamination_status=None,
        )

    @staticmethod
    def _split_legenda(legenda: Any) -> tuple[Optional[str], Optional[str]]:
        """Split "Kwetsbaar gebied - 40-60 %" into its class and its age band."""
        if not isinstance(legenda, str) or " - " not in legenda:
            return (legenda.strip().lower() if isinstance(legenda, str) else None), None
        klass, _, band = legenda.partition(" - ")
        return klass.strip().lower(), band.strip()

    @staticmethod
    def _ground_is_vulnerable(
        vulnerability: Optional[str], soil_region: Optional[str], province: Optional[str]
    ) -> bool:
        """Whether the ground itself is a concern, before considering the house."""
        if vulnerability == "kwetsbaar gebied":
            return True
        # Soft, wet ground is a concern whatever the area class says — this is
        # also what carries the "Overig" class, which states no verdict itself.
        if soil_region and soil_region.lower() in HIGH_RISK_SOIL_REGIONS:
            return True
        # Cities carry no soil class; the dataset says to judge them by region.
        if vulnerability == "stedelijk gebied":
            return bool(province and province.strip().lower() in VULNERABLE_URBAN_PROVINCES)
        return False

    @classmethod
    def _risk_ordinal(
        cls,
        vulnerability: Optional[str],
        soil_region: Optional[str],
        province: Optional[str],
        pre_1970: bool,
    ) -> Optional[int]:
        """Combine ground vulnerability with the building's era.

        Neither factor alone is the hazard: pile rot needs both a foundation
        old enough to be timber and ground where the water table moves. A
        modern house on bad ground has deep concrete piles; an old house on
        sand was never at risk. Written as a table rather than as arithmetic
        because the reasoning has to stay auditable.
        """
        if vulnerability is None:
            return None

        vulnerable = cls._ground_is_vulnerable(vulnerability, soil_region, province)
        peat = bool(soil_region and soil_region.lower() in PEAT_REGIONS)

        if not pre_1970:
            # Post-1970 foundations are deep concrete; ground matters far less.
            return 2 if vulnerability == "kwetsbaar gebied" else 1 if vulnerable else 0
        if not vulnerable:
            # Old and shallow, but on ground the dataset says is not a concern.
            return 1
        if vulnerability == "kwetsbaar gebied":
            return 4 if peat else 3
        # Vulnerable urban ground, pre-1970: the classic wooden-pile case.
        return 3

    @classmethod
    def _paalrot(
        cls,
        vulnerability: Optional[str],
        soil_region: Optional[str],
        province: Optional[str],
        pre_1970: bool,
    ) -> Optional[bool]:
        if vulnerability is None:
            return None
        return bool(pre_1970 and cls._ground_is_vulnerable(vulnerability, soil_region, province))


def _as_float(value: Any) -> Optional[float]:
    try:
        return round(float(value), 1) if value is not None else None
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
