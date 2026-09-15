"""Market pressure: average sale prices against WOZ assessments.

See :mod:`app.models.market` for what this measures and, importantly, what it
does not — the true overbidding percentage is not open data.

A note on CBS dimension keys, because this is the third distinct convention in
the codebase. ``WijkenEnBuurten`` is padded to 10 characters, ``SoortMisdrijf``
to 6, and ``RegioS`` in table 83625NED is **unpadded**. Getting it wrong yields
an empty 200, not an error. Each is pinned next to the table it belongs to.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from app.core.http_client import HttpClient
from app.models.market import MarketPoint, MarketTrend
from app.services.enrichment.providers.cbs_odata import OPENDATA_BASE
from app.services.woz import WozLookup

log = logging.getLogger(__name__)

#: Bestaande koopwoningen; gemiddelde verkoopprijzen, regio. Annual, from 1995,
#: covering 728 municipalities.
SALE_PRICE_DATASET = "83625NED"
SALE_PRICE_FIELD = "GemiddeldeVerkoopprijs_1"

#: RegioS in this table carries no padding, unlike WijkenEnBuurten elsewhere.
REGIO_IS_UNPADDED = True

DEFAULT_YEARS = 7


class MarketLookup:
    """Combines the sale-price series with the WOZ series for a municipality."""

    def __init__(self, http: HttpClient, base_url: str = OPENDATA_BASE) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.woz = WozLookup(http, base_url)

    async def trend(
        self,
        gemeentecode: str,
        *,
        years: int = DEFAULT_YEARS,
        asking_price_eur: Optional[int] = None,
        gemeente_name: Optional[str] = None,
    ) -> MarketTrend:
        if not gemeentecode:
            return MarketTrend()

        sales, woz = await asyncio.gather(
            self._sale_prices(gemeentecode),
            self.woz.trend(gemeentecode, years=years),
            return_exceptions=True,
        )
        if isinstance(sales, BaseException):
            log.warning("sale-price lookup failed for %s: %s", gemeentecode, sales)
            sales = {}
        if isinstance(woz, BaseException):
            log.warning("WOZ lookup failed for %s: %s", gemeentecode, woz)
            woz_by_year: dict[int, int] = {}
        else:
            woz_by_year = {p.year: p.value_eur for p in woz.points}

        wanted = sorted(set(sales) | set(woz_by_year))[-years:]
        points = [
            MarketPoint(year=year,
                        average_sale_eur=sales.get(year),
                        average_woz_eur=woz_by_year.get(year))
            for year in wanted
        ]

        return MarketTrend(
            gemeente_code=gemeentecode,
            gemeente_name=gemeente_name,
            points=points,
            asking_price_eur=asking_price_eur,
        )

    async def _sale_prices(self, gemeentecode: str) -> dict[int, int]:
        """Average sale price per year for one municipality."""
        payload = await self.http.get_json(
            f"{self.base_url}/{SALE_PRICE_DATASET}/TypedDataSet",
            params={
                "$format": "json",
                "$select": f"RegioS,Perioden,{SALE_PRICE_FIELD}",
                "$filter": f"RegioS eq '{gemeentecode.strip()}'",
                "$top": 200,
            },
            headers={"Accept": "application/json"},
        )
        out: dict[int, int] = {}
        for row in payload.get("value") or []:
            value = row.get(SALE_PRICE_FIELD)
            period = str(row.get("Perioden") or "").strip()
            if value is None or len(period) < 4 or not period[:4].isdigit():
                continue
            out[int(period[:4])] = int(value)
        return out
