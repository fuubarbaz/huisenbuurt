"""Registered crime — Politie open data, published through CBS "dataderden".

Table 47022NED carries *monthly* counts at buurt level, which is the finest
grain available and the reason it is preferred over the gemeente-level tables.

Two deliberate choices:

* **Twelve-month sums, not single months.** A buurt records single-digit counts
  per month; month-to-month noise would dominate any score built on it. Summing
  a rolling 12 months also cancels seasonality (burglary peaks in winter).
* **Rates, not counts.** Counts are normalised per 1000 inhabitants using the
  population from Kerncijfers wijken en buurten, then expressed as a ratio
  against the national rate — a buurt is "1.8x the national burglary rate",
  which survives comparison across areas of very different size.

Known limitation
----------------
Crime is registered where it *happens*, but the denominator is who *lives*
there. City-centre, nightlife and retail buurten therefore score far worse than
a resident experiences: a few hundred residents absorb the recorded crime of
tens of thousands of daily visitors. The effect is largest for the nuisance and
violence groups and smallest for residential burglary, which is why burglary
carries the most weight in the safety dimension. Treat centre-of-town figures
as an upper bound rather than a resident's exposure.
"""
from __future__ import annotations

import logging
from typing import Any, Optional, Sequence

from app.core.config import settings
from app.core.http_client import HttpClient
from app.models.enrichment import CrimeStats, MonthlyCount
from app.services.enrichment.providers.base import BaseProvider, EnrichmentContext, NoDataFound
from app.services.enrichment.providers.cbs_odata import (
    AREA_KEY_WIDTH,
    CRIME_KEY_WIDTH,
    DATADERDEN_BASE,
    NATIONAL_CODE,
    CBSODataClient,
    any_of,
)

log = logging.getLogger(__name__)

F_COUNT = "GeregistreerdeMisdrijven_1"
D_AREA = "WijkenEnBuurten"
D_CRIME = "SoortMisdrijf"
D_PERIOD = "Perioden"

TOTAL_CODE = "0.0.0"

#: High-impact family metrics, mapped to Politie "SoortMisdrijf" codes.
#: Several are groups: nuisance and violence have no single code.
OFFENCE_GROUPS: dict[str, tuple[str, ...]] = {
    "burglaries": ("1.1.1", "1.1.2"),   # woning + box/garage/schuur
    "vandalism": ("2.2.1",),            # vernieling cq. zaakbeschadiging
    "nuisance": (                       # the "overlast" family
        "2.1.1",                        #   drugs/drankoverlast
        "2.4.1",                        #   burengerucht (relatieproblemen)
        "2.7.3",                        #   leefbaarheid (overig)
        "3.6.4",                        #   aantasting openbare orde
    ),
    "violent": ("1.4.3", "1.4.4", "1.4.5", "1.4.6", "1.4.7"),
}

#: Every code the provider asks for, plus the all-crime total.
ALL_CODES: tuple[str, ...] = (TOTAL_CODE, *sorted({c for g in OFFENCE_GROUPS.values() for c in g}))

WINDOW_MONTHS = 12
#: Two windows: the trailing year, and the year before it for the trend.
LOOKBACK_MONTHS = WINDOW_MONTHS * 2

# Population comes from Kerncijfers wijken en buurten, on the other CBS host.
F_POPULATION = "AantalInwoners_5"


class PolitieProvider(BaseProvider[CrimeStats]):
    name = "politie"
    source_url = "https://data.politie.nl/"

    def __init__(
        self,
        http: HttpClient,
        dataset: str | None = None,
        kwb_dataset: str | None = None,
    ) -> None:
        super().__init__(http)
        self.client = CBSODataClient(
            http, dataset or settings.politie_crime_dataset, base_url=DATADERDEN_BASE
        )
        # Separate table, separate host — crime counts carry no population.
        self.kwb = CBSODataClient(http, kwb_dataset or settings.cbs_kwb_dataset)

    async def fetch(self, ctx: EnrichmentContext) -> CrimeStats:
        chain = [c for c in (ctx.buurtcode, ctx.geo.wijkcode, ctx.gemeentecode) if c]
        if not chain:
            raise NoDataFound("no CBS area code resolved for this address")

        areas = [*chain, NATIONAL_CODE]
        periods = await self._recent_periods()
        if not periods:
            raise NoDataFound("no periods published in the crime table")

        counts = await self._crime_counts(areas, periods)
        if not counts:
            raise NoDataFound(f"no crime rows for {areas}")

        current, previous = periods[-WINDOW_MONTHS:], periods[:-WINDOW_MONTHS]

        area_code = self._first_area_with_data(chain, counts, current)
        if area_code is None:
            raise NoDataFound(f"no crime data at any level of {chain}")

        populations = await self._populations(areas)
        population = populations.get(area_code)
        if not population:
            raise NoDataFound(f"no population for {area_code}; cannot normalise counts")

        national_pop = populations.get(NATIONAL_CODE)
        return self._to_model(
            area_code, counts, current, previous, population,
            national_pop=national_pop,
        )

    # -- upstream reads ----------------------------------------------------

    async def _recent_periods(self) -> list[str]:
        """The most recent published months, oldest first.

        Read from the table's own Perioden dimension rather than generated from
        today's date: the newest month is published on a lag, and asking for a
        month that does not exist yet silently narrows the window.
        """
        keys = [k for k in await self.client.dimension_keys(D_PERIOD) if "MM" in k]
        return sorted(keys)[-LOOKBACK_MONTHS:]

    async def _crime_counts(
        self, areas: Sequence[str], periods: Sequence[str]
    ) -> dict[tuple[str, str, str], int]:
        """One request for every area x offence x month, keyed for lookup."""
        filt = " and ".join([
            any_of(D_AREA, areas, AREA_KEY_WIDTH),
            any_of(D_CRIME, ALL_CODES, CRIME_KEY_WIDTH),
            any_of(D_PERIOD, periods, len(periods[0])),
        ])
        rows = await self.client.query(
            filter=filt, select=[D_AREA, D_CRIME, D_PERIOD, F_COUNT]
        )
        return {
            (r[D_AREA], r[D_CRIME], r[D_PERIOD]): r[F_COUNT]
            for r in rows
            if r.get(F_COUNT) is not None
        }

    async def _populations(self, areas: Sequence[str]) -> dict[str, int]:
        """Inhabitants per area, for per-1000 normalisation."""
        try:
            rows = await self.kwb.rows_for_areas(areas, [F_POPULATION])
        except Exception as exc:  # noqa: BLE001 — a rate we cannot normalise is not fatal
            log.warning("population lookup failed: %s", exc)
            return {}
        return {code: row[F_POPULATION] for code, row in rows.items() if row.get(F_POPULATION)}

    # -- shaping -----------------------------------------------------------

    @staticmethod
    def _first_area_with_data(
        chain: Sequence[str],
        counts: dict[tuple[str, str, str], int],
        window: Sequence[str],
    ) -> Optional[str]:
        """Finest area level that reported any total-crime figure in the window."""
        for code in chain:
            if any((code, TOTAL_CODE, p) in counts for p in window):
                return code
        return None

    @staticmethod
    def _sum(
        counts: dict[tuple[str, str, str], int],
        area: str,
        codes: Sequence[str],
        window: Sequence[str],
    ) -> Optional[int]:
        vals = [
            counts[(area, code, period)]
            for code in codes
            for period in window
            if (area, code, period) in counts
        ]
        return sum(vals) if vals else None

    def _rate(
        self,
        counts: dict[tuple[str, str, str], int],
        area: str,
        codes: Sequence[str],
        window: Sequence[str],
        population: int,
    ) -> Optional[float]:
        total = self._sum(counts, area, codes, window)
        if total is None or not population:
            return None
        return round(1000.0 * total / population, 2)

    def _to_model(
        self,
        area_code: str,
        counts: dict[tuple[str, str, str], int],
        current: Sequence[str],
        previous: Sequence[str],
        population: int,
        *,
        national_pop: Optional[int],
    ) -> CrimeStats:
        rate = lambda codes: self._rate(counts, area_code, codes, current, population)  # noqa: E731

        total_rate = rate((TOTAL_CODE,))

        # Trend: this year's total against last year's, same area.
        now_total = self._sum(counts, area_code, (TOTAL_CODE,), current)
        prev_total = self._sum(counts, area_code, (TOTAL_CODE,), previous)
        trend = (
            round(100.0 * (now_total - prev_total) / prev_total, 1)
            if now_total is not None and prev_total else None
        )

        # National comparison, computed on rates so size differences cancel.
        # Each offence group is compared against *its own* national rate, so
        # "1.8x national" means the same thing for burglary and for vandalism.
        national_ratio = None
        category_ratios: dict[str, float] = {}
        if national_pop:
            def national_rate(codes: Sequence[str]) -> Optional[float]:
                return self._rate(counts, NATIONAL_CODE, codes, current, national_pop)

            nat_total = national_rate((TOTAL_CODE,))
            if total_rate is not None and nat_total:
                national_ratio = round(total_rate / nat_total, 2)

            for group, codes in OFFENCE_GROUPS.items():
                local, national = rate(codes), national_rate(codes)
                if local is not None and national:
                    category_ratios[group] = round(local / national, 2)

        # Absolute counts and the monthly series: the provider already has 24
        # months in hand, and throwing them away to keep only ratios loses the
        # answer to "what actually happened here recently".
        counts_12m = {
            group: total
            for group, codes in OFFENCE_GROUPS.items()
            if (total := self._sum(counts, area_code, codes, current)) is not None
        }
        monthly = [
            MonthlyCount(period=period, count=counts[(area_code, TOTAL_CODE, period)])
            for period in current
            if (area_code, TOTAL_CODE, period) in counts
        ]

        return CrimeStats(
            area_code=area_code,
            area_level=_level_of(area_code),
            period=f"{current[0]}..{current[-1]}" if current else None,
            burglaries_per_1000=rate(OFFENCE_GROUPS["burglaries"]),
            vandalism_per_1000=rate(OFFENCE_GROUPS["vandalism"]),
            nuisance_reports_per_1000=rate(OFFENCE_GROUPS["nuisance"]),
            violent_crime_per_1000=rate(OFFENCE_GROUPS["violent"]),
            total_registered_per_1000=total_rate,
            trend_12m_pct=trend,
            national_average_ratio=national_ratio,
            category_ratios=category_ratios,
            counts_12m=counts_12m,
            monthly_totals=monthly,
        )


def _level_of(area_code: str) -> str:
    return {"BU": "buurt", "WK": "wijk", "GM": "gemeente", "NL": "land"}.get(
        area_code[:2].upper(), "onbekend"
    )
