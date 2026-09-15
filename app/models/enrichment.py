"""Per-layer enrichment models.

Every provider returns exactly one of these payloads, wrapped in a
``ProviderResult`` envelope so the pipeline can degrade gracefully: a dead
upstream API becomes a MISSING result with a reason, never an exception that
kills the whole run.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, ClassVar, Generic, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field, computed_field

from app.models.property import GeoIdentity

PayloadT = TypeVar("PayloadT", bound=BaseModel)


class ProviderStatus(str, Enum):
    OK = "ok"
    PARTIAL = "partial"          # some fields resolved, some upstream gaps
    MISSING = "missing"          # upstream had no data for this location
    ERROR = "error"              # upstream failed (timeout, 5xx, parse error)
    SKIPPED = "skipped"          # not applicable (e.g. foundation check on new-build)


class ProviderResult(BaseModel, Generic[PayloadT]):
    """Uniform envelope around every enrichment layer."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    provider: str
    status: ProviderStatus
    payload: Optional[PayloadT] = None
    error: Optional[str] = None
    source_url: Optional[str] = None
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    duration_ms: Optional[int] = None

    # computed_field, not a bare property: clients decide whether to render a
    # layer from this, and model_dump drops a plain @property — the same trap
    # that made WozTrend's percentages arrive as absent.
    @computed_field  # type: ignore[prop-decorator]
    @property
    def usable(self) -> bool:
        return self.status in (ProviderStatus.OK, ProviderStatus.PARTIAL) and self.payload is not None


# --------------------------------------------------------------------------
# Layer payloads
# --------------------------------------------------------------------------


class LeefbaarometerData(BaseModel):
    """Leefbaarometer 3.0 livability classification for the property's cell."""

    score: Optional[float] = Field(None, description="Composite livability score (lbm).")
    klasse: Optional[str] = Field(None, description="e.g. 'Ruim voldoende', 'Zwak'.")
    klasse_ordinal: Optional[int] = Field(
        None, ge=1, le=9, description="1=Zeer onvoldoende .. 9=Uitstekend."
    )
    dimensions: dict[str, float] = Field(
        default_factory=dict,
        description=(
            "Raw sub-scores: woningvoorraad, fysieke_omgeving, voorzieningen, "
            "sociale_samenhang, veiligheid."
        ),
    )
    dimension_classes: dict[str, int] = Field(
        default_factory=dict, description="The same five dimensions as 1-9 classes."
    )
    deviation: Optional[float] = Field(
        None, description="Afwijking: difference from the score expected for this area type."
    )
    aggregation_level: Optional[str] = Field(
        None, description="'grid' (100m) | 'buurt' | 'wijk' | 'gemeente' — what was served."
    )
    area_name: Optional[str] = None
    reference_year: Optional[int] = None


class MonthlyCount(BaseModel):
    """One month of recorded crime, for drawing a trend."""

    period: str = Field(..., description="CBS period key, e.g. '2026MM07'.")
    count: int

    @property
    def label(self) -> str:
        """'2026MM07' -> '2026-07'."""
        return self.period.replace("MM", "-")


class CrimeStats(BaseModel):
    """Politie / CBS registered-crime counts for the surrounding area."""

    area_code: Optional[str] = None
    area_level: Optional[str] = Field(None, description="'buurt' | 'wijk' | 'gemeente'.")
    period: Optional[str] = Field(
        None, description="The summed window, e.g. '2025MM08..2026MM07'."
    )

    burglaries_per_1000: Optional[float] = None
    vandalism_per_1000: Optional[float] = None
    nuisance_reports_per_1000: Optional[float] = Field(None, description="Overlast meldingen.")
    violent_crime_per_1000: Optional[float] = None
    total_registered_per_1000: Optional[float] = None

    trend_12m_pct: Optional[float] = Field(None, description="YoY change, negative = improving.")
    national_average_ratio: Optional[float] = Field(
        None, description="Area rate / national rate, all crime. <1.0 is safer than average."
    )
    category_ratios: dict[str, float] = Field(
        default_factory=dict,
        description="Per-offence-group rate / national rate for the same group.",
    )
    counts_12m: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Absolute offences recorded in the area over the window, per group. "
            "A rate answers 'how does this compare'; a count answers 'how much "
            "actually happened here', which is the question a buyer asks."
        ),
    )
    monthly_totals: list[MonthlyCount] = Field(
        default_factory=list,
        description="All recorded crime by month, oldest first — the shape of the trend.",
    )


class DemographicsData(BaseModel):
    """CBS Kerncijfers wijken en buurten — the family-concentration layer."""

    buurtcode: Optional[str] = Field(None, description="The area the figures actually came from.")
    area_level: Optional[str] = Field(
        None, description="'buurt' | 'wijk' | 'gemeente' — which fallback level resolved."
    )
    households_total: Optional[int] = None
    households_with_children_pct: Optional[float] = Field(None, ge=0, le=100)
    population_total: Optional[int] = None
    age_0_15_pct: Optional[float] = Field(None, ge=0, le=100)
    avg_household_size: Optional[float] = None

    age_bands_pct: dict[str, float] = Field(
        default_factory=dict,
        description="Share of residents per CBS age band — 0-15, 15-25, 25-45, 45-65, 65+.",
    )
    single_person_households_pct: Optional[float] = Field(None, ge=0, le=100)
    households_without_children_pct: Optional[float] = Field(None, ge=0, le=100)

    owner_occupied_pct: Optional[float] = Field(None, ge=0, le=100)
    rental_pct: Optional[float] = Field(None, ge=0, le=100)
    social_housing_pct: Optional[float] = Field(
        None, ge=0, le=100, description="Share held by a housing corporation."
    )
    single_family_homes_pct: Optional[float] = Field(
        None, ge=0, le=100, description="Eengezinswoningen; the rest are flats."
    )
    address_density_per_km2: Optional[int] = None
    urbanity_class: Optional[int] = Field(None, ge=1, le=5, description="1=very urban .. 5=rural.")

    # Child-friendliness proximity signals, free in the same KWB row.
    distance_to_gp_km: Optional[float] = None
    distance_to_supermarket_km: Optional[float] = None
    distance_to_daycare_km: Optional[float] = None
    distance_to_school_km: Optional[float] = None

    children_pct_vs_national: Optional[float] = Field(
        None, description="Area share / national share. >1.0 = more families than average."
    )
    reference_year: Optional[int] = None


class School(BaseModel):
    brin: Optional[str] = Field(None, description="DUO BRIN institution number.")
    name: str
    denomination: Optional[str] = Field(None, description="Denominatie, e.g. 'Openbaar'.")
    education_type: Optional[str] = Field(None, description="'po' | 'vo' | 'so'.")
    distance_m: Optional[int] = None
    inspection_rating: Optional[str] = Field(
        None, description="Onderwijsinspectie verdict: 'Goed' | 'Voldoende' | 'Onvoldoende' | 'Zeer zwak'."
    )
    pupil_count: Optional[int] = None


class EducationData(BaseModel):
    schools: list[School] = Field(default_factory=list)
    nearest_primary_distance_m: Optional[int] = None
    primary_schools_within_1km: Optional[int] = None
    pct_rated_good_or_better: Optional[float] = Field(
        None, ge=0, le=100, description="Share of *rated* nearby schools at Voldoende or better."
    )
    schools_rated: int = Field(0, description="How many nearby schools carry an actual verdict.")
    poorly_rated_nearby: list[str] = Field(
        default_factory=list, description="Nearby schools rated below Voldoende."
    )
    denominations: list[str] = Field(
        default_factory=list, description="Distinct denominations within walking distance."
    )
    ratings_as_of: Optional[str] = Field(
        None, description="Snapshot date of the inspection verdicts (DUO publishes 2018 only)."
    )
    search_radius_m: Optional[int] = None


class NoiseData(BaseModel):
    """RIVM / Atlas Leefomgeving geluidbelastingkaarten (Lden / Lnight in dB)."""

    road_lden_db: Optional[float] = None
    road_lnight_db: Optional[float] = None
    rail_lden_db: Optional[float] = None
    rail_lnight_db: Optional[float] = None
    aviation_lden_db: Optional[float] = None
    industry_lden_db: Optional[float] = None
    cumulative_lden_db: Optional[float] = Field(
        None, description="All sources combined; RIVM's own figure where available."
    )
    sources_mapped: int = Field(
        0,
        description=(
            "How many of road/rail/aviation/industry had a mapped value. Zero "
            "with an OK status means genuinely quiet, not missing data."
        ),
    )

    @property
    def worst_lden_db(self) -> Optional[float]:
        vals = [
            v
            for v in (self.road_lden_db, self.rail_lden_db, self.aviation_lden_db, self.industry_lden_db)
            if v is not None
        ]
        return max(vals) if vals else None


class SoilRiskData(BaseModel):
    """Klimaateffectatlas + Bodemloket: the structural-security layer."""

    foundation_risk_class: Optional[str] = Field(
        None, description="Funderingsrisico class: 'geen'|'laag'|'matig'|'hoog'|'zeer hoog'."
    )
    foundation_risk_ordinal: Optional[int] = Field(None, ge=0, le=4, description="0=none .. 4=severe.")
    paalrot_risk: Optional[bool] = Field(
        None, description="Wooden-pile rot exposure: vulnerable ground AND pre-1970 construction."
    )
    soil_type: Optional[str] = Field(
        None, description="Fysisch-geografische regio, e.g. 'Laagveengebied', 'Hogere Zandgronden'."
    )
    soil_vulnerability: Optional[str] = Field(
        None, description="'kwetsbaar gebied' | 'stedelijk gebied' | 'niet kwetsbaar gebied'."
    )
    area_pre_1970_pct: Optional[float] = Field(
        None, ge=0, le=100, description="Share of buildings in this PC6 built before 1970."
    )
    area_buildings: Optional[int] = Field(None, description="BAG building count in this PC6.")
    area_postcode: Optional[str] = None
    construction_year_known: bool = Field(
        True,
        description=(
            "False when the risk was derived from the area's building-age mix "
            "rather than this property's own construction year."
        ),
    )

    flood_risk_ordinal: Optional[int] = Field(
        None, ge=1, le=5,
        description=(
            "RIVM's national flood-probability class for this point, 1 (does not "
            "flood) to 5 (roughly once per 10 years), given current flood defences."
        ),
    )
    flood_risk_class: Optional[str] = Field(
        None, description="RIVM's own label for the class, e.g. '1x per 100 jaar'."
    )
    flood_depth_m: Optional[float] = Field(
        None, description="Not populated: no national depth source is currently reachable."
    )
    subsidence_mm_per_year: Optional[float] = Field(
        None, description="Not populated: no national subsidence service is currently reachable."
    )
    contamination_status: Optional[str] = Field(
        None, description="Not populated: every published Bodemloket endpoint is dead."
    )
    contamination_investigations: int = 0


class AirQualityData(BaseModel):
    """RIVM annual-mean background concentrations, in µg/m³.

    Sampled from the same Atlas Leefomgeving WMS as the noise maps, at the
    property's own coordinate. These are modelled background concentrations
    (the NSL/GCN grid), not a kerbside measurement: a flat on a busy canal will
    read close to its neighbourhood rather than to the traffic lane outside.

    Compared against the WHO 2021 guidelines rather than the EU limit values,
    which are far weaker — the EU annual limit for NO2 is 40 µg/m³ against
    WHO's 10, so scoring on the EU figure would call almost every Dutch address
    clean and tell a buyer nothing.
    """

    no2_ug_m3: Optional[float] = Field(None, description="Nitrogen dioxide, annual mean.")
    pm25_ug_m3: Optional[float] = Field(None, description="Fine particulate, annual mean.")
    pm10_ug_m3: Optional[float] = Field(None, description="Coarse particulate, annual mean.")

    reference: Optional[str] = Field(
        None, description="Which RIVM edition answered; the aliases move with the data."
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def pollutants_mapped(self) -> int:
        """How many of the three had a value. Zero with an OK status would be
        odd — these grids cover the whole country — so it flags a bad sample
        rather than a clean address."""
        return sum(1 for v in (self.no2_ug_m3, self.pm25_ug_m3, self.pm10_ug_m3)
                   if v is not None)


class EnrichmentBundle(BaseModel):
    """Everything the pipeline knows about one property's location.

    Kept deliberately flat and JSON-serialisable: this is the object the
    scoring engine consumes and the Phase 2 REST layer returns verbatim.
    """

    property_id: str
    geo: Optional[GeoIdentity] = None

    leefbaarometer: ProviderResult[LeefbaarometerData]
    crime: ProviderResult[CrimeStats]
    demographics: ProviderResult[DemographicsData]
    education: ProviderResult[EducationData]
    noise: ProviderResult[NoiseData]
    air: ProviderResult[AirQualityData]
    soil: ProviderResult[SoilRiskData]

    enriched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    total_duration_ms: Optional[int] = None

    #: The layer names, in one place. Coverage, failures and the pipeline's
    #: backfill all read this — three hardcoded copies is how a newly added
    #: layer ends up silently excluded from the coverage percentage.
    LAYER_NAMES: ClassVar[tuple[str, ...]] = (
        "leefbaarometer", "crime", "demographics", "education", "noise", "air", "soil",
    )

    def layers(self) -> list[ProviderResult]:
        return [getattr(self, name) for name in self.LAYER_NAMES]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def coverage_pct(self) -> float:
        """Share of layers that returned usable data — feeds score confidence."""
        results = self.layers()
        return round(100.0 * sum(1 for r in results if r.usable) / len(results), 1)

    def failures(self) -> dict[str, str]:
        out: dict[str, Any] = {}
        for name in self.LAYER_NAMES:
            r: ProviderResult = getattr(self, name)
            if not r.usable:
                out[name] = r.error or r.status.value
        return out
