"""Central configuration. Every upstream endpoint lives here so a moved API is
a one-line change, never a code change inside a provider."""
from __future__ import annotations

from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="WOONAGENT_", extra="ignore")

    # --- storage -----------------------------------------------------------
    database_url: str = "sqlite+aiosqlite:///./data/woonagent.db"
    # A relative path here is a trap this codebase has already been bitten by
    # once (see WOONAGENT_DATABASE_URL above and fly.toml): on Fly the
    # writable volume is mounted at /data, not ./data relative to the app's
    # working directory, so a bare relative default would silently write
    # outside the persistent volume and lose a build on every redeploy.
    national_map_db_path: str = "data/national_map.duckdb"

    # --- scraping politeness ----------------------------------------------
    scrape_interval_min_seconds: int = Field(300, description="Lower bound of the 5-15 min poll jitter.")
    scrape_interval_max_seconds: int = Field(900, description="Upper bound of the 5-15 min poll jitter.")
    request_delay_min_seconds: float = 2.0
    request_delay_max_seconds: float = 6.0
    max_retries: int = 4
    backoff_base_seconds: float = 1.5
    backoff_max_seconds: float = 60.0
    http_timeout_seconds: float = 20.0

    #: Upper bound on listings processed in one cycle. Without it a first run
    #: against a busy day would spend hours before the user sees anything.
    max_listings_per_cycle: int = 25

    # --- enrichment concurrency -------------------------------------------
    enrichment_provider_timeout_seconds: float = 15.0
    enrichment_max_concurrency: int = 6

    # --- notifications -----------------------------------------------------
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    min_score_to_notify: float = 6.5

    # --- filters -----------------------------------------------------------
    max_price_eur: Optional[int] = None
    min_living_area_m2: Optional[int] = None
    # Comma-separated cities and/or postcode prefixes, e.g. "Amsterdam,1018".
    # Unset means unfiltered — the whole country — which is why a filter is
    # worth setting before leaving the daemon running unattended.
    regions: Optional[str] = None

    # --- upstream endpoints ------------------------------------------------
    pdok_locatieserver_url: str = "https://api.pdok.nl/bzk/locatieserver/search/v3_1"
    pdok_wijkenbuurten_wfs: str = "https://service.pdok.nl/cbs/wijkenbuurten/2024/wfs/v1_0"
    leefbaarometer_wms: str = "https://geo.leefbaarometer.nl/geoserver/wms"
    leefbaarometer_edition: str = Field("24", description="Two-digit edition year, e.g. '24' = 2024.")
    cbs_odata_url: str = "https://datasets.cbs.nl/odata/v1/CBS"
    cbs_kwb_dataset: str = Field("86165NED", description="Kerncijfers wijken en buurten 2025.")
    politie_crime_dataset: str = Field(
        "47022NED", description="Politie geregistreerde misdrijven; wijk, buurt, maand."
    )
    duo_opendata_base: str = "https://duo.nl/open_onderwijsdata"
    rivm_noise_wms: str = "https://data.rivm.nl/geo/alo/wms"
    # Same Atlas Leefomgeving server as the noise maps, named separately so a
    # move of one does not silently drag the other with it.
    rivm_air_wms: str = "https://data.rivm.nl/geo/alo/wms"
    # BAG: the national building registry. Construction year and floor area,
    # free and keyless — the authoritative answer to what a listing page states.
    bag_wfs: str = "https://service.pdok.nl/lv/bag/wfs/v2_0"

    # EP-Online: RVO's energy-label register. Needs a free API key; without one
    # the label is simply absent rather than an error.
    eponline_url: str = "https://public.ep-online.nl/api/v5/PandEnergielabel"
    eponline_api_key: Optional[str] = None

    # Routing. Car (OSRM) and bike (BRouter) need no key. Public transport does:
    # no keyless door-to-door transit router exists for the Netherlands.
    ors_api_key: Optional[str] = None

    # Foundation risk: RVO's indicative problem areas, PC6 polygons on PDOK.
    funderingsrisico_wfs: str = (
        "https://service.pdok.nl/rvo/indicatieve-aandachtsgebieden-funderingsproblematiek/wfs/v1_0"
    )
    # RIVM's national flood-probability grid — same Atlas Leefomgeving server
    # as the noise and air-quality layers, kept as its own setting so moving
    # one does not silently drag the others with it.
    rivm_flood_wms: str = "https://data.rivm.nl/geo/alo/wms"

    # Identify the agent honestly to the sites it reads. A contact address lets
    # an operator reach you rather than silently blocking you.
    user_agent: Optional[str] = None
    contact_email: Optional[str] = None
    respect_robots: bool = Field(True, description="Never disable this for a third-party site.")

    log_level: str = "INFO"
    dry_run: bool = Field(False, description="Run the pipeline but suppress outbound notifications.")

    # --- public exposure ---------------------------------------------------
    # Unset locally, so nothing changes on a laptop. Set it on any host that is
    # reachable from the internet: one /score is roughly a dozen calls to PDOK,
    # CBS, the BAG and two volunteer-run routing servers, which makes an open
    # endpoint an amplifier pointed at other people's free infrastructure.
    access_token: Optional[str] = Field(
        None, description="When set, every route except /health requires this token."
    )

    # Bounds the actual cost driver on a hosted deployment: a machine that
    # never goes idle never scales to zero, and /score alone is roughly a
    # dozen upstream calls. Generous enough for interactive use, tight enough
    # that sustained hammering (a leaked token, a misbehaving script) cannot
    # turn into an unbounded bill. Applies everywhere the app runs, but only
    # matters where idle time is money — i.e. on Fly, not on a laptop.
    rate_limit_per_minute: float = Field(30.0, description="Per-client request budget.")
    rate_limit_burst: int = Field(10, description="Requests allowed in a sudden burst.")


settings = Settings()
