"""Listing-level models. These are the contract between the scrapers, the
database and (in Phase 2) the FastAPI response layer."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ListingSource(str, Enum):
    FUNDA = "funda"
    HUISPEDIA = "huispedia"
    NVM_BROKER = "nvm_broker"
    MANUAL = "manual"


class PropertyListing(BaseModel):
    """A raw listing as harvested by a scraper, before any enrichment."""

    model_config = ConfigDict(str_strip_whitespace=True)

    property_id: str = Field(..., description="Stable per-source unique id; DB primary key.")
    source: ListingSource
    url: str
    address: str
    postal_code: str = Field(..., description="PC6, e.g. '1015AA'.")
    house_number: str
    house_number_addition: Optional[str] = None
    city: Optional[str] = None

    price_eur: Optional[int] = None
    living_area_m2: Optional[int] = None
    plot_area_m2: Optional[int] = None
    rooms: Optional[int] = None
    construction_year: Optional[int] = None

    listed_at: Optional[datetime] = None
    scraped_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("postal_code")
    @classmethod
    def _normalise_pc6(cls, v: str) -> str:
        compact = v.replace(" ", "").upper()
        if len(compact) != 6 or not compact[:4].isdigit() or not compact[4:].isalpha():
            raise ValueError(f"Invalid Dutch PC6 postal code: {v!r}")
        return compact

    @property
    def pc4(self) -> str:
        return self.postal_code[:4]

    @property
    def price_per_m2(self) -> Optional[float]:
        if self.price_eur and self.living_area_m2:
            return round(self.price_eur / self.living_area_m2, 2)
        return None

    @property
    def is_pre_1970(self) -> bool:
        """Pre-1970 stock drives the foundation/paalrot deep-check."""
        return self.construction_year is not None and self.construction_year < 1970


class GeoIdentity(BaseModel):
    """Output of the PDOK Locatieserver lookup: the spatial keys every
    downstream open-data layer is joined on."""

    latitude: float
    longitude: float
    rd_x: Optional[float] = Field(None, description="Rijksdriehoek X (EPSG:28992).")
    rd_y: Optional[float] = Field(None, description="Rijksdriehoek Y (EPSG:28992).")

    buurtcode: Optional[str] = Field(None, description="CBS buurt code, e.g. 'BU03630001'.")
    buurtnaam: Optional[str] = None
    wijkcode: Optional[str] = Field(None, description="CBS wijk code, e.g. 'WK036301'.")
    gemeentecode: Optional[str] = Field(None, description="CBS gemeente code, e.g. 'GM0363'.")
    gemeentenaam: Optional[str] = None
    provincie: Optional[str] = None

    bag_nummeraanduiding_id: Optional[str] = None
    bag_verblijfsobject_id: Optional[str] = Field(
        None,
        description=(
            "BAG id of this specific dwelling. Distinguishes one flat from "
            "another inside the same building, which a spatial query cannot."
        ),
    )
    matched_address: Optional[str] = None
    match_score: Optional[float] = Field(None, description="Locatieserver relevance score.")
    exact_house_number: bool = Field(
        True,
        description=(
            "False when the geocoder matched a different house number within the "
            "correct postcode — the location is right to within a PC6, not exact."
        ),
    )
