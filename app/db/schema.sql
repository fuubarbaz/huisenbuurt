-- WoonAgent SQLite schema.
-- property_id is the primary key throughout: it is what makes re-running the
-- scraper idempotent and what guarantees one alert per home, ever.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS properties (
    property_id             TEXT PRIMARY KEY,
    source                  TEXT NOT NULL,
    url                     TEXT NOT NULL,
    address                 TEXT NOT NULL,
    postal_code             TEXT NOT NULL,
    house_number            TEXT NOT NULL,
    house_number_addition   TEXT,
    city                    TEXT,
    price_eur               INTEGER,
    living_area_m2          INTEGER,
    plot_area_m2            INTEGER,
    rooms                   INTEGER,
    construction_year       INTEGER,
    listed_at               TEXT,
    first_seen_at           TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at            TEXT NOT NULL DEFAULT (datetime('now')),
    status                  TEXT NOT NULL DEFAULT 'new'
        CHECK (status IN ('new','enriched','scored','notified','failed','ignored'))
);

CREATE INDEX IF NOT EXISTS idx_properties_status ON properties(status);
CREATE INDEX IF NOT EXISTS idx_properties_pc ON properties(postal_code);

-- Geocoding is cached permanently: an address's coordinates never change.
CREATE TABLE IF NOT EXISTS geo_identity (
    property_id         TEXT PRIMARY KEY REFERENCES properties(property_id) ON DELETE CASCADE,
    latitude            REAL NOT NULL,
    longitude           REAL NOT NULL,
    rd_x                REAL,
    rd_y                REAL,
    buurtcode           TEXT,
    buurtnaam           TEXT,
    wijkcode            TEXT,
    gemeentecode        TEXT,
    gemeentenaam        TEXT,
    provincie           TEXT,
    matched_address     TEXT,
    resolved_at         TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_geo_buurt ON geo_identity(buurtcode);

-- The full EnrichmentBundle, stored as JSON. Area-level layers are re-usable
-- across listings in the same buurt; see enrichment_cache below.
CREATE TABLE IF NOT EXISTS enrichment (
    property_id     TEXT PRIMARY KEY REFERENCES properties(property_id) ON DELETE CASCADE,
    bundle_json     TEXT NOT NULL,
    coverage_pct    REAL,
    enriched_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Area-keyed cache so ten listings in one buurt cost one CBS call, not ten.
CREATE TABLE IF NOT EXISTS enrichment_cache (
    provider        TEXT NOT NULL,
    area_key        TEXT NOT NULL,  -- buurtcode, gemeentecode, or a rounded lat/lon cell
    payload_json    TEXT NOT NULL,
    fetched_at      TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at      TEXT,
    PRIMARY KEY (provider, area_key)
);

CREATE TABLE IF NOT EXISTS scores (
    property_id         TEXT PRIMARY KEY REFERENCES properties(property_id) ON DELETE CASCADE,
    total_score         REAL NOT NULL,
    confidence          REAL,
    dimensions_json     TEXT NOT NULL,
    risk_flags_json     TEXT NOT NULL,
    scored_at           TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_scores_total ON scores(total_score DESC);

-- The duplicate-alert guard: a row here means the user has already been told.
CREATE TABLE IF NOT EXISTS notifications (
    property_id     TEXT NOT NULL REFERENCES properties(property_id) ON DELETE CASCADE,
    channel         TEXT NOT NULL DEFAULT 'telegram',
    sent_at         TEXT NOT NULL DEFAULT (datetime('now')),
    success         INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (property_id, channel)
);
