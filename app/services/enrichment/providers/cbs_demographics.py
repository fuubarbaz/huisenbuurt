"""CBS Kerncijfers wijken en buurten — household and age composition.

Supplies the peer/family-concentration dimension: what share of households in
this buurt have children, and how many under-15s live there.

CBS blanks figures for areas too small to publish safely. Measured against
KWB 2025, 632 of 14,729 buurten (4.3%) carry no household count — uncommon,
but not rare enough to leave unhandled. The provider walks buurt -> wijk ->
gemeente and records which level it landed on, because "35% of households here
have children" means something quite different at buurt scale than averaged
across all of Amsterdam.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from app.core.config import settings
from app.core.http_client import HttpClient
from app.models.enrichment import DemographicsData
from app.services.enrichment.providers.base import BaseProvider, EnrichmentContext, NoDataFound
from app.services.enrichment.providers.cbs_odata import NATIONAL_CODE, CBSODataClient

log = logging.getLogger(__name__)

# Field keys in Kerncijfers wijken en buurten. The numeric suffixes are part of
# the column name and change between annual editions, so they are pinned here
# next to the dataset id they belong to.
F_POPULATION = "AantalInwoners_5"
F_AGE_0_15 = "k_0Tot15Jaar_8"
F_AGE_15_25 = "k_15Tot25Jaar_9"
F_AGE_25_45 = "k_25Tot45Jaar_10"
F_AGE_45_65 = "k_45Tot65Jaar_11"
F_AGE_65_PLUS = "k_65JaarOfOuder_12"
F_SINGLE_PERSON = "Eenpersoonshuishoudens_30"
F_NO_CHILDREN = "HuishoudensZonderKinderen_31"
F_OWNER_OCCUPIED = "Koopwoningen_47"
F_RENTAL = "HuurwoningenTotaal_48"
F_SOCIAL_HOUSING = "InBezitWoningcorporatie_49"
F_SINGLE_FAMILY = "PercentageEengezinswoning_40"
F_MULTI_FAMILY = "PercentageMeergezinswoning_45"

#: Age bands as CBS publishes them, in order.
AGE_BANDS: list[tuple[str, str]] = [
    ("0-15", F_AGE_0_15), ("15-25", F_AGE_15_25), ("25-45", F_AGE_25_45),
    ("45-65", F_AGE_45_65), ("65+", F_AGE_65_PLUS),
]

# Income is deliberately absent. GemiddeldInkomenPerInwoner and the two income
# quantile columns exist in this edition but are null for every buurt checked —
# the same empty-column trap as the gas-use field. A field that is always None
# is worse than no field.
F_HOUSEHOLDS = "HuishoudensTotaal_29"
F_HOUSEHOLDS_KIDS = "HuishoudensMetKinderen_32"
F_HOUSEHOLD_SIZE = "GemiddeldeHuishoudensgrootte_33"
F_URBANITY = "MateVanStedelijkheid_120"
F_ADDRESS_DENSITY = "Omgevingsadressendichtheid_121"
# The "Nabijheid voorzieningen" group. Free in the sense that matters: they
# ride along in the row this provider already reads, so they cost no extra
# request. Daycare and school feed the family and education dimensions; the
# other two are shown to the buyer but deliberately not scored, because
# "how far is the supermarket" is a preference, not a health or safety fact.
F_DIST_GP = "AfstandTotHuisartsenpraktijk_110"
F_DIST_SUPERMARKET = "AfstandTotGroteSupermarkt_111"
F_DIST_DAYCARE = "AfstandTotKinderdagverblijf_112"
F_DIST_SCHOOL = "AfstandTotSchool_113"

SELECT_FIELDS = [
    F_POPULATION, F_AGE_0_15, F_HOUSEHOLDS, F_HOUSEHOLDS_KIDS, F_HOUSEHOLD_SIZE,
    F_URBANITY, F_ADDRESS_DENSITY, F_DIST_GP, F_DIST_SUPERMARKET,
    F_DIST_DAYCARE, F_DIST_SCHOOL,
    F_AGE_15_25, F_AGE_25_45, F_AGE_45_65, F_AGE_65_PLUS,
    F_SINGLE_PERSON, F_NO_CHILDREN,
    F_OWNER_OCCUPIED, F_RENTAL, F_SOCIAL_HOUSING,
    F_SINGLE_FAMILY, F_MULTI_FAMILY,
]

# A row is only useful if it carries the two fields the family dimension needs.
REQUIRED_FIELDS = (F_HOUSEHOLDS, F_HOUSEHOLDS_KIDS)


class CBSDemographicsProvider(BaseProvider[DemographicsData]):
    name = "cbs_demographics"
    source_url = "https://www.cbs.nl/nl-nl/reeksen/kerncijfers-wijken-en-buurten"

    def __init__(self, http: HttpClient, dataset: str | None = None) -> None:
        super().__init__(http)
        self.client = CBSODataClient(http, dataset or settings.cbs_kwb_dataset)

    async def fetch(self, ctx: EnrichmentContext) -> DemographicsData:
        if not ctx.buurtcode and not ctx.gemeentecode:
            raise NoDataFound("no CBS area code resolved for this address")

        # Request the whole fallback chain plus the national baseline in one
        # round-trip; we need at most all of them and often more than one.
        chain = [c for c in (ctx.buurtcode, ctx.geo.wijkcode, ctx.gemeentecode) if c]
        rows = await self.client.rows_for_areas(
            [*chain, NATIONAL_CODE], SELECT_FIELDS
        )
        if not rows:
            raise NoDataFound(f"no KWB row for any of {chain}")

        area_code, row = self._first_usable(chain, rows)
        if row is None:
            raise NoDataFound(f"all of {chain} were suppressed or absent")

        national = rows.get(NATIONAL_CODE)
        return self._to_model(area_code, row, national)

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _first_usable(
        chain: list[str], rows: dict[str, dict[str, Any]]
    ) -> tuple[Optional[str], Optional[dict[str, Any]]]:
        """Walk buurt -> wijk -> gemeente, taking the finest level with data."""
        for code in chain:
            row = rows.get(code)
            if row and all(row.get(f) is not None for f in REQUIRED_FIELDS):
                return code, row
            if row:
                log.debug("CBS suppressed %s for %s; falling back", REQUIRED_FIELDS, code)
        return None, None

    def _to_model(
        self,
        area_code: str,
        row: dict[str, Any],
        national: Optional[dict[str, Any]],
    ) -> DemographicsData:
        pct = self.client.pct
        kids_pct = pct(row.get(F_HOUSEHOLDS_KIDS), row.get(F_HOUSEHOLDS))

        national_pct = (
            pct(national.get(F_HOUSEHOLDS_KIDS), national.get(F_HOUSEHOLDS))
            if national else None
        )
        ratio = (
            round(kids_pct / national_pct, 2)
            if kids_pct is not None and national_pct else None
        )

        return DemographicsData(
            buurtcode=area_code,
            area_level=_level_of(area_code),
            households_total=row.get(F_HOUSEHOLDS),
            households_with_children_pct=kids_pct,
            population_total=row.get(F_POPULATION),
            age_0_15_pct=pct(row.get(F_AGE_0_15), row.get(F_POPULATION)),
            avg_household_size=row.get(F_HOUSEHOLD_SIZE),
            address_density_per_km2=row.get(F_ADDRESS_DENSITY),
            urbanity_class=row.get(F_URBANITY),
            distance_to_gp_km=row.get(F_DIST_GP),
            distance_to_supermarket_km=row.get(F_DIST_SUPERMARKET),
            distance_to_daycare_km=row.get(F_DIST_DAYCARE),
            distance_to_school_km=row.get(F_DIST_SCHOOL),
            # Counts become shares of the population, so one area compares
            # with another regardless of size.
            age_bands_pct={
                band: share for band, field in AGE_BANDS
                if (share := pct(row.get(field), row.get(F_POPULATION))) is not None
            },
            single_person_households_pct=pct(row.get(F_SINGLE_PERSON), row.get(F_HOUSEHOLDS)),
            households_without_children_pct=pct(row.get(F_NO_CHILDREN), row.get(F_HOUSEHOLDS)),
            owner_occupied_pct=row.get(F_OWNER_OCCUPIED),
            rental_pct=row.get(F_RENTAL),
            social_housing_pct=row.get(F_SOCIAL_HOUSING),
            single_family_homes_pct=row.get(F_SINGLE_FAMILY),
            children_pct_vs_national=ratio,
            reference_year=_year_of(self.client.dataset),
        )


def _level_of(area_code: str) -> str:
    return {"BU": "buurt", "WK": "wijk", "GM": "gemeente", "NL": "land"}.get(
        area_code[:2].upper(), "onbekend"
    )


def _year_of(dataset: str) -> Optional[int]:
    """KWB table ids carry no year, so the mapping is pinned explicitly.

    An unknown id yields None rather than a guess — a wrong reference year is
    worse than an absent one when comparing areas across editions.
    """
    return KWB_DATASET_YEARS.get(dataset.upper())


#: Kerncijfers wijken en buurten table ids by reference year.
KWB_DATASET_YEARS: dict[str, int] = {
    "85984NED": 2024,
    "86165NED": 2025,
}
