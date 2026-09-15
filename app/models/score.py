"""Scoring output models."""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from app.models.enrichment import EnrichmentBundle
from app.models.property import PropertyListing
from app.models.market import MarketTrend
from app.models.building import BuildingFacts
from app.models.woz import WozTrend


class ScoreDimension(str, Enum):
    SAFETY = "physical_safety"
    FAMILY = "peer_family_concentration"
    EDUCATION = "education_quality_access"
    ENVIRONMENT = "environmental_health"
    STRUCTURAL = "structural_security"


class DimensionScore(BaseModel):
    dimension: ScoreDimension
    score: float = Field(..., ge=0.0, le=10.0)
    weight: float = Field(..., ge=0.0, le=1.0)
    confidence: float = Field(1.0, ge=0.0, le=1.0, description="Drops when inputs were imputed.")
    imputed: bool = False
    drivers: list[str] = Field(default_factory=list, description="Human-readable reasons.")

    @property
    def weighted(self) -> float:
        return self.score * self.weight


class RiskFlag(BaseModel):
    code: str = Field(..., description="Stable machine code, e.g. 'HIGH_ROAD_NOISE'.")
    severity: str = Field(..., description="'info' | 'warn' | 'critical'.")
    message: str


class HolisticScore(BaseModel):
    property_id: str
    total_score: float = Field(..., ge=1.0, le=10.0)
    dimensions: list[DimensionScore]
    risk_flags: list[RiskFlag] = Field(default_factory=list)
    confidence: float = Field(..., ge=0.0, le=1.0, description="Weighted data coverage.")
    data_coverage_pct: float


class ScoredProperty(BaseModel):
    """The full record: listing + enrichment + score.

    This is the exact shape the Phase 2 `GET /properties/{id}` endpoint returns,
    and the exact shape the Telegram formatter consumes. Neither one owns it.
    """

    listing: PropertyListing
    enrichment: Optional[EnrichmentBundle] = None
    score: Optional[HolisticScore] = None
    building: Optional[BuildingFacts] = Field(
        None,
        description=(
            "What the registries know: construction year and floor area from "
            "BAG, energy label and house type from EP-Online. The renovation "
            "estimate is requested separately, from POST /renovation, because "
            "it depends on the target label the buyer chooses."
        ),
    )
    market: Optional[MarketTrend] = Field(
        None,
        description=(
            "Municipal sale prices against WOZ assessments — a proxy for market "
            "pressure. Not the true overbidding percentage, which is not open data."
        ),
    )
    woz: Optional[WozTrend] = Field(
        None,
        description=(
            "Municipal valuation trend for the surrounding area. Context, not a "
            "scoring input — how a neighbourhood is priced says little about "
            "whether it suits a family, which is what the five dimensions measure."
        ),
    )
