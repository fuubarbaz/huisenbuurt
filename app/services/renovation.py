"""Indicative renovation cost for a property.

Reads the local cost table built by ``scripts/build_renovation_costs.py`` from
Verbeterjehuis.nl (Milieu Centraal), so scoring a property never touches their
site. Their figures are attributed wherever they are shown.

What this is
------------
An **indication**, built from three things this engine already knows: the
building's construction year (BAG), its floor area (BAG), and its energy label
where an EP-Online key is configured. It answers "roughly what would bringing
this house up to standard cost, and what would it save" — the question you ask
before booking a viewing, not the one you ask before signing.

What it is not
--------------
A quote. Real cost depends on the state of the fabric, access, the contractor,
and what has already been done — none of which is in any registry. The tables
are national averages by house type. For a real figure the house needs a
*maatwerkadvies*, and the estimate links to Verbeterjehuis's own tool for that.

Which measures apply
--------------------
The honest form of the question is *from here to there*: what does it cost to
take this house from its current label to the one you want. That is a set
difference — the measures a G-rated house still needs, minus the ones a B-rated
house still needs, is the work between them.

Where no target is given the estimator falls back to "bring it up to standard",
using the label if there is one and otherwise the construction year, against
the Dutch regulatory timeline:

* **before 1976** — no insulation requirement existed; assume all measures.
* **1976-1991** — first requirements, thin by modern standards; roof and floor
  typically still worthwhile, glazing often single or early double.
* **1992-2005** — insulation standard, glazing usually double; little left.
* **after 2005** — built to a modern standard; nothing assumed.

These are era heuristics, not a survey, and the estimate says so.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field, computed_field

log = logging.getLogger(__name__)

DEFAULT_TABLE_PATH = Path("data/renovation_costs.json")
ADVICE_URL = "https://www.verbeterjehuis.nl/"

#: House types the published tables are broken down by.
HOUSE_TYPES = ("terraced", "end_terrace", "semi_detached", "detached")

#: Measures assumed still outstanding, by construction era. See the module
#: docstring for the regulatory timeline these follow.
ERA_MEASURES: list[tuple[int, tuple[str, ...]]] = [
    (1976, ("gevelisolatie", "vloerisolatie", "dakisolatie", "isolatieglas")),
    (1992, ("vloerisolatie", "dakisolatie", "isolatieglas")),
    (2006, ("isolatieglas",)),
]

#: Energy label to outstanding measures, used in preference to the era when a
#: label is known. A/B are modern-standard; G is uninsulated.
LABEL_MEASURES: dict[str, tuple[str, ...]] = {
    "G": ("gevelisolatie", "vloerisolatie", "dakisolatie", "isolatieglas"),
    "F": ("gevelisolatie", "vloerisolatie", "dakisolatie", "isolatieglas"),
    "E": ("vloerisolatie", "dakisolatie", "isolatieglas"),
    "D": ("vloerisolatie", "dakisolatie"),
    "C": ("vloerisolatie",),
    "B": (),
    "A": (),
}


class MeasureEstimate(BaseModel):
    measure: str
    title: str
    url: str
    cost_low_eur: int
    cost_high_eur: int
    subsidy_eur: Optional[int] = None
    saving_eur_year: Optional[int] = None


class RenovationEstimate(BaseModel):
    """Indicative cost of bringing a property up to standard."""

    basis: str = Field(..., description="'energy label' | 'construction year' | 'none'.")
    energy_label: Optional[str] = None
    target_label: Optional[str] = Field(
        None, description="The label asked for, when the estimate is a from-to."
    )
    construction_year: Optional[int] = None
    house_type: Optional[str] = Field(
        None, description="None means the type is unknown and a range is given."
    )
    is_apartment: bool = Field(
        False,
        description=(
            "True when the dwelling shares a building. The published tables "
            "price whole houses, and in a flat the roof, facade and floor "
            "belong to the VvE rather than to you — so the figures are shown "
            "as building-level context, not as your bill."
        ),
    )
    measures: list[MeasureEstimate] = Field(default_factory=list)
    source: Optional[str] = None
    source_url: str = ADVICE_URL

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_low_eur(self) -> int:
        return sum(m.cost_low_eur for m in self.measures)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_high_eur(self) -> int:
        return sum(m.cost_high_eur for m in self.measures)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_subsidy_eur(self) -> int:
        return sum(m.subsidy_eur or 0 for m in self.measures)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_saving_eur_year(self) -> int:
        return sum(m.saving_eur_year or 0 for m in self.measures)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def payback_years(self) -> Optional[float]:
        """Net of subsidy, at the midpoint of the cost range."""
        saving = self.total_saving_eur_year
        if not saving:
            return None
        midpoint = (self.total_low_eur + self.total_high_eur) / 2 - self.total_subsidy_eur
        return round(midpoint / saving, 1) if midpoint > 0 else 0.0


class RenovationEstimator:
    """Applies the cost table to one property."""

    def __init__(self, table_path: Path | str = DEFAULT_TABLE_PATH) -> None:
        self.table_path = Path(table_path)
        self._table: Optional[dict] = None

    @property
    def available(self) -> bool:
        return self.table_path.exists()

    def _load(self) -> dict:
        if self._table is None:
            self._table = json.loads(self.table_path.read_text())
        return self._table

    def between(
        self,
        *,
        current_label: str,
        target_label: str,
        house_type: Optional[str] = None,
        units_in_building: Optional[int] = None,
    ) -> Optional[RenovationEstimate]:
        """Cost of moving from one label to another.

        The work between two labels is what the worse one still needs minus
        what the better one still needs. Asking to go *down* a grade, or to
        stay where you are, costs nothing rather than erroring.
        """
        if not self.available:
            return None

        current = _measures_for_label(current_label)
        target = _measures_for_label(target_label)
        wanted = tuple(m for m in current if m not in target)

        return self._build(
            wanted, basis="label target", energy_label=current_label,
            target_label=target_label, house_type=house_type,
            units_in_building=units_in_building,
        )

    def estimate(
        self,
        *,
        construction_year: Optional[int] = None,
        energy_label: Optional[str] = None,
        house_type: Optional[str] = None,
        units_in_building: Optional[int] = None,
    ) -> Optional[RenovationEstimate]:
        """None when the table has not been built, or nothing applies."""
        if not self.available:
            log.debug("no renovation table at %s", self.table_path)
            return None

        wanted, basis = self._applicable(construction_year, energy_label)
        return self._build(
            wanted, basis=basis, energy_label=energy_label,
            construction_year=construction_year, house_type=house_type,
            units_in_building=units_in_building,
        )

    def _build(
        self,
        wanted: tuple[str, ...],
        *,
        basis: str,
        energy_label: Optional[str] = None,
        target_label: Optional[str] = None,
        construction_year: Optional[int] = None,
        house_type: Optional[str] = None,
        units_in_building: Optional[int] = None,
    ) -> RenovationEstimate:
        """Price a set of measures for one property."""
        table = self._load()
        is_apartment = bool(units_in_building and units_in_building > 1)
        types = (house_type,) if house_type in HOUSE_TYPES else HOUSE_TYPES

        measures: list[MeasureEstimate] = []
        for entry in table.get("measures", []):
            if entry["measure"] not in wanted:
                continue
            costs = [entry["by_house_type"][t] for t in types
                     if t in entry["by_house_type"]]
            if not costs:
                continue
            amounts = [c["cost_eur"] for c in costs]
            cheapest = costs[amounts.index(min(amounts))]
            measures.append(MeasureEstimate(
                measure=entry["measure"], title=entry["title"], url=entry["url"],
                cost_low_eur=min(amounts), cost_high_eur=max(amounts),
                # Subsidy and saving are quoted for the same house type as the
                # low end, so the figures stay internally consistent.
                subsidy_eur=cheapest.get("subsidy_eur"),
                saving_eur_year=cheapest.get("saving_eur_year"),
            ))

        return RenovationEstimate(
            basis=basis, energy_label=energy_label, target_label=target_label,
            construction_year=construction_year,
            house_type=house_type if house_type in HOUSE_TYPES else None,
            is_apartment=is_apartment, measures=measures,
            source=table.get("source"),
            source_url=table.get("source_url", ADVICE_URL),
        )

    @staticmethod
    def _applicable(
        construction_year: Optional[int], energy_label: Optional[str]
    ) -> tuple[tuple[str, ...], str]:
        """Outstanding measures, preferring the label over the era."""
        if energy_label:
            # "A+++" and "A" alike are modern standard.
            letter = energy_label.strip().upper()[:1]
            if letter in LABEL_MEASURES:
                return LABEL_MEASURES[letter], "energy label"
        if construction_year:
            for cutoff, measures in ERA_MEASURES:
                if construction_year < cutoff:
                    return measures, "construction year"
            return (), "construction year"
        return (), "none"


def _measures_for_label(label: Optional[str]) -> tuple[str, ...]:
    """Measures a house at this label still has outstanding."""
    letter = (label or "").strip().upper()[:1]
    return LABEL_MEASURES.get(letter, ())
