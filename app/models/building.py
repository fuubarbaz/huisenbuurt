"""Building facts from the national registries.

In `app/models/` rather than beside the lookup service so the dependency runs
one way: models are leaves, services import them. Defining it in the service
made ``app.models.score`` import ``app.services``, which closed a cycle back
through the enrichment package — the same mistake as ``WozTrend``, which lives
here for the same reason.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class BuildingFacts(BaseModel):
    """What the registries know about the building at an address."""

    construction_year: Optional[int] = None
    unit_construction_year: Optional[int] = None
    floor_area_m2: Optional[int] = None
    floor_area_is_exact: bool = True
    units_in_building: Optional[int] = None
    use: Optional[str] = None
    status: Optional[str] = None
    energy_label: Optional[str] = None
    energy_label_valid_until: Optional[str] = None
    energy_label_registered: Optional[str] = None
    house_type: Optional[str] = Field(
        None, description="From the register's Gebouwtype; keyed to the cost tables."
    )

    @property
    def has_anything(self) -> bool:
        return any((self.construction_year, self.floor_area_m2, self.energy_label))
