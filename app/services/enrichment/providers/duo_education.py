"""Schools near the property — DUO open onderwijsdata + Onderwijsinspectie.

Reads the locally cached, geocoded registry rather than the network; see
:mod:`app.services.enrichment.school_index` for why, and
``scripts/build_school_index.py`` for how it is built. That makes this the one
provider that does no I/O at request time, and the one that can be unavailable
for a reason the operator controls: if the index has not been built, the layer
reports MISSING with instructions rather than silently scoring zero.

On the inspection verdicts
--------------------------
DUO's verdict file is a **2018 snapshot** — the only published one — and 81% of
primary schools in it are simply "Voldoende", with 73 "Goed" and 114 below par
out of 7,142. So the ratings barely rank schools, and they are eight years old.

They are therefore used as a *flag*, not as the backbone of the score: a nearby
school rated Onvoldoende or Zeer zwak is worth telling a buyer about, but the
dimension is built mainly on proximity and choice, which are current and do
discriminate. Every rating carries ``rating_as_of`` so its age travels with it.
"""
from __future__ import annotations

import logging
from collections import Counter
from pathlib import Path
from typing import Optional

from app.models.enrichment import EducationData, School
from app.services.enrichment.providers.base import BaseProvider, EnrichmentContext, NoDataFound
from app.services.enrichment.school_index import (
    DEFAULT_INDEX_PATH,
    IndexedSchool,
    SchoolIndex,
    SchoolIndexMissing,
)

log = logging.getLogger(__name__)

SEARCH_RADIUS_M = 3000
WALKABLE_RADIUS_M = 1000
MAX_SCHOOLS_REPORTED = 8

#: Verdicts that carry no information about quality — treated as unrated
#: rather than as bad, which is a different thing entirely.
NON_VERDICTS = {"geen oordeel", "zonder actueel oordeel", ""}

#: Onderwijsinspectie verdicts, worst to best. "Zwak" belongs to the older
#: supervision framework and sits between the current "Onvoldoende" and
#: "Voldoende"; it appears on only 19 primary schools in the snapshot.
RATING_ORDER = ["zeer zwak", "onvoldoende", "zwak", "voldoende", "goed"]
GOOD_RATINGS = {"voldoende", "goed"}
POOR_RATINGS = {"onvoldoende", "zeer zwak", "zwak"}


class DUOEducationProvider(BaseProvider[EducationData]):
    name = "duo_education"
    source_url = "https://duo.nl/open_onderwijsdata/"

    def __init__(self, http, index_path: Path | str = DEFAULT_INDEX_PATH) -> None:
        super().__init__(http)
        self.index = SchoolIndex(index_path)

    async def fetch(self, ctx: EnrichmentContext) -> EducationData:
        lat, lon = ctx.lat_lon
        try:
            nearby = self.index.nearby(lat, lon, radius_m=SEARCH_RADIUS_M)
        except SchoolIndexMissing as exc:
            raise NoDataFound(str(exc)) from exc

        if not nearby:
            raise NoDataFound(f"no primary school within {SEARCH_RADIUS_M} m")

        schools = [_to_school(s) for s in nearby[:MAX_SCHOOLS_REPORTED]]
        rated = [_norm(s.rating) for s in nearby if _norm(s.rating) not in NON_VERDICTS]

        walkable = [s for s in nearby if s.distance_m <= WALKABLE_RADIUS_M]

        return EducationData(
            schools=schools,
            nearest_primary_distance_m=nearby[0].distance_m,
            primary_schools_within_1km=len(walkable),
            pct_rated_good_or_better=(
                round(100.0 * sum(1 for r in rated if r in GOOD_RATINGS) / len(rated), 1)
                if rated else None
            ),
            schools_rated=len(rated),
            poorly_rated_nearby=[
                s.name for s in nearby if _norm(s.rating) in POOR_RATINGS
            ],
            denominations=sorted({s.denomination for s in walkable if s.denomination}),
            ratings_as_of=next((s.rating_as_of for s in nearby if s.rating_as_of), None),
            search_radius_m=SEARCH_RADIUS_M,
        )


def _norm(rating: Optional[str]) -> str:
    return (rating or "").strip().lower()


def _to_school(s: IndexedSchool) -> School:
    return School(
        brin=s.brin,
        name=s.name,
        denomination=s.denomination or None,
        education_type=s.education_type,
        distance_m=s.distance_m,
        inspection_rating=s.rating if _norm(s.rating) not in NON_VERDICTS else None,
    )
