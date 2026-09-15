"""WOZ valuation models.

Kept here rather than beside the lookup service so the dependency runs one way:
models are leaves, services import them. Putting them in the service made
``app.models.score`` import ``app.services``, which closed a cycle back through
the enrichment package.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, computed_field


class WozPoint(BaseModel):
    year: int
    value_eur: int = Field(..., description="Average WOZ per dwelling, in euros.")


class WozTrend(BaseModel):
    """Average WOZ over time for one CBS area."""

    area_code: Optional[str] = None
    area_level: Optional[str] = Field(None, description="'buurt' | 'wijk' | 'gemeente'.")
    points: list[WozPoint] = Field(default_factory=list)
    missing_years: list[int] = Field(
        default_factory=list,
        description="Editions where this area code does not appear — a boundary change, not a zero.",
    )

    @property
    def latest(self) -> Optional[WozPoint]:
        return self.points[-1] if self.points else None

    # computed_field, not a bare property: a plain @property is invisible to
    # model_dump, so these would reach the API as absent and render as "—".
    @computed_field  # type: ignore[prop-decorator]
    @property
    def change_pct(self) -> Optional[float]:
        """Change across the whole span, first published year to last."""
        if len(self.points) < 2:
            return None
        first, last = self.points[0].value_eur, self.points[-1].value_eur
        return round(100.0 * (last - first) / first, 1) if first else None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def change_pct_1y(self) -> Optional[float]:
        if len(self.points) < 2:
            return None
        prev, last = self.points[-2].value_eur, self.points[-1].value_eur
        return round(100.0 * (last - prev) / prev, 1) if prev else None
