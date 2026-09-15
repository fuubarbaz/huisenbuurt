"""WOZ valuation trend for the area around a property.

The WOZ (Waardering Onroerende Zaken) is the municipal valuation every Dutch
dwelling carries. CBS publishes the average per area in each annual Kerncijfers
wijken en buurten edition, so querying one area code across editions gives a
genuine multi-year trend from documented open data.

Per-address history is *not* read here. The WOZ-waardeloket shows a value for
your own house, but it has no documented public API — only the endpoints its
own single-page app calls — and guessing at those is the kind of undocumented
scraping this codebase declines elsewhere. The area average is published for
exactly this purpose and is what a buyer wants anyway: whether the
neighbourhood is appreciating, not what one house was assessed at.

Boundaries move
---------------
CBS redraws buurt and wijk boundaries, and renumbers them when it does.
Amsterdam's codes break between the 2022 and 2023 editions: ``BU0363AC01``
simply does not exist in 2021, nor does its wijk. Only the gemeente code
survives.

So the series is built from **one area code across editions**, and a year where
that code is absent is a gap, not a substitution. Silently falling back to the
gemeente for the missing years would draw a line through two different
geographies and call it a trend.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

from app.core.http_client import HttpClient
from app.models.woz import WozPoint, WozTrend
from app.services.enrichment.providers.cbs_odata import OPENDATA_BASE, pad_area

log = logging.getLogger(__name__)

#: Kerncijfers wijken en buurten editions, newest last. Only editions with a
#: stable per-year table are listed; the pre-2013 ones cover year ranges.
KWB_EDITIONS: dict[int, str] = {
    2016: "83487NED", 2017: "83765NED", 2018: "84286NED", 2019: "84583NED",
    2020: "84799NED", 2021: "85039NED", 2022: "85318NED", 2023: "85618NED",
    2024: "85984NED", 2025: "86165NED",
}

#: The WOZ column name carries an index that changes between editions. These
#: are the verified ones; anything else is discovered from DataProperties and
#: memoised, so an unknown edition costs one extra call per process, not per
#: property.
WOZ_FIELD: dict[str, str] = {
    "84583NED": "GemiddeldeWOZWaardeVanWoningen_35",
    "84799NED": "GemiddeldeWOZWaardeVanWoningen_35",
    "85039NED": "GemiddeldeWOZWaardeVanWoningen_35",
    "85318NED": "GemiddeldeWOZWaardeVanWoningen_35",
    "85618NED": "GemiddeldeWOZWaardeVanWoningen_36",
    "85984NED": "GemiddeldeWOZWaardeVanWoningen_39",
    "86165NED": "GemiddeldeWOZWaardeVanWoningen_39",
}

DEFAULT_YEARS = 7


class WozLookup:
    """Reads the WOZ series for an area from the CBS KWB editions."""

    def __init__(self, http: HttpClient, base_url: str = OPENDATA_BASE) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")

    async def trend(
        self, area_code: str, *, years: int = DEFAULT_YEARS
    ) -> WozTrend:
        """Average WOZ for one area across the most recent editions."""
        if not area_code:
            return WozTrend()

        editions = dict(sorted(KWB_EDITIONS.items())[-years:])
        results = await asyncio.gather(
            *(self._value(dataset, area_code) for dataset in editions.values()),
            return_exceptions=True,
        )

        points: list[WozPoint] = []
        missing: list[int] = []
        for year, outcome in zip(editions, results):
            if isinstance(outcome, BaseException):
                log.warning("WOZ lookup failed for %s in %d: %s", area_code, year, outcome)
                missing.append(year)
            elif outcome is None:
                missing.append(year)
            else:
                points.append(WozPoint(year=year, value_eur=outcome))

        return WozTrend(
            area_code=area_code,
            area_level=_level_of(area_code),
            points=points,
            missing_years=missing,
        )

    async def _value(self, dataset: str, area_code: str) -> Optional[int]:
        field = await self._field_for(dataset)
        if not field:
            return None
        payload = await self.http.get_json(
            f"{self.base_url}/{dataset}/TypedDataSet",
            params={
                "$format": "json",
                "$select": f"WijkenEnBuurten,{field}",
                "$filter": f"WijkenEnBuurten eq '{pad_area(area_code)}'",
            },
            headers={"Accept": "application/json"},
        )
        rows = payload.get("value") or []
        if not rows or rows[0].get(field) is None:
            return None
        # CBS publishes the figure in thousands of euros.
        return int(round(float(rows[0][field]) * 1000))

    async def _field_for(self, dataset: str) -> Optional[str]:
        """The WOZ column for an edition, discovered once and memoised."""
        if dataset in WOZ_FIELD:
            return WOZ_FIELD[dataset]
        payload = await self.http.get_json(
            f"{self.base_url}/{dataset}/DataProperties",
            params={"$format": "json"}, headers={"Accept": "application/json"},
        )
        field = next(
            (p["Key"] for p in payload.get("value") or []
             if p.get("Key") and re.search(r"woz", str(p.get("Title") or ""), re.I)),
            None,
        )
        if field:
            WOZ_FIELD[dataset] = field
        else:
            log.warning("no WOZ column found in %s", dataset)
        return field


def _level_of(area_code: str) -> str:
    return {"BU": "buurt", "WK": "wijk", "GM": "gemeente"}.get(
        area_code[:2].upper(), "onbekend")
