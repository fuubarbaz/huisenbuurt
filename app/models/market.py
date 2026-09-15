"""Market-pressure models.

What this is, and what it is not
--------------------------------
It is **not** the overbidding percentage. That figure — what buyers actually
paid above the asking price — needs both numbers for the same transaction, and
only one of them is public. CBS discontinued its asking-price series after
2016Q4, and it held price *bands* rather than averages even then. NVM publishes
quarterly overbidding statistics, but as PDF market reports with no API.
Kadaster records every transaction and licenses the data commercially. A search
of the CBS catalogue for "overbieden" returns nothing at all.

What is published, per municipality and back to 1995, is the **average sale
price**; and per area per year, the **average WOZ assessment**. Their ratio is a
usable stand-in for market heat, because overbidding is what happens when
buyers pay well above assessed value. The series tracks the real market: it
peaks at 1.42-1.49 across 2021-22 — the height of Dutch overbidding — falls to
1.10-1.20 in 2023 as the market corrected, and has been flat since.

Read the *movement*, not the level. The WOZ has a waardepeildatum of 1 January
of the preceding year, so a 2025 sale is compared against a valuation eighteen
months older. That lag alone puts the ratio above 1.0 in any rising market, so
the absolute number overstates overbidding; the direction and the turning
points are the signal.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, computed_field


class MarketPoint(BaseModel):
    year: int
    average_sale_eur: Optional[int] = None
    average_woz_eur: Optional[int] = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sale_to_woz(self) -> Optional[float]:
        """Average sale price over average assessed value."""
        if not self.average_sale_eur or not self.average_woz_eur:
            return None
        return round(self.average_sale_eur / self.average_woz_eur, 3)


class MarketTrend(BaseModel):
    """Sale prices and assessments over time for one municipality."""

    gemeente_code: Optional[str] = None
    gemeente_name: Optional[str] = None
    points: list[MarketPoint] = Field(default_factory=list)

    #: The listing's own asking price, compared with the area average.
    asking_price_eur: Optional[int] = None

    @property
    def latest(self) -> Optional[MarketPoint]:
        for point in reversed(self.points):
            if point.sale_to_woz is not None:
                return point
        return None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def pressure_now(self) -> Optional[float]:
        """Most recent sale-to-assessment ratio."""
        latest = self.latest
        return latest.sale_to_woz if latest else None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def pressure_peak(self) -> Optional[float]:
        ratios = [p.sale_to_woz for p in self.points if p.sale_to_woz is not None]
        return max(ratios) if ratios else None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def asking_vs_area_pct(self) -> Optional[float]:
        """How far the asking price sits above or below the area's average sale.

        A direct, current comparison — unlike the ratio above it needs no
        registry lag caveat, though it does compare one specific house against
        an average of every dwelling type in the municipality.
        """
        latest = self.latest
        if not self.asking_price_eur or not latest or not latest.average_sale_eur:
            return None
        return round(
            100.0 * (self.asking_price_eur - latest.average_sale_eur)
            / latest.average_sale_eur, 1
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sale_price_change_pct(self) -> Optional[float]:
        """Change in the average sale price across the published span."""
        sales = [p.average_sale_eur for p in self.points if p.average_sale_eur]
        if len(sales) < 2 or not sales[0]:
            return None
        return round(100.0 * (sales[-1] - sales[0]) / sales[0], 1)
