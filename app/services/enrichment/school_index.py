"""Local school index: a cached, geocoded copy of the DUO school registry.

Why an index rather than live calls
-----------------------------------
DUO publishes the registry as bulk CSV with no coordinates and no spatial
query interface — 6,096 primary schools, addressed by postcode. Answering
"which schools are near this house" per property would mean downloading the
whole registry and geocoding it every time. So the registry is geocoded once
into SQLite (see ``scripts/build_school_index.py``) and queried locally, which
turns a multi-minute operation into a sub-millisecond one.

The index is a cache, not state: deleting it costs a rebuild, nothing more.

Positional accuracy
-------------------
DUO gives addresses, not coordinates, so each school is geocoded from its
postcode at house number 1. A Dutch PC6 typically covers one side of one
street, so positions are good to roughly 50-100 m and distances should be read
as "about 600 m", never as surveyed values. Two addresses sharing a postcode
can legitimately come out 0 m apart.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

# Re-exported: haversine_m has lived in this module's namespace since the
# index was written, and the repository needs the same implementation.
from app.core.geometry import EARTH_RADIUS_M, haversine_m  # noqa: F401

DEFAULT_INDEX_PATH = Path("data/schools.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS schools (
    vestigingscode  TEXT PRIMARY KEY,
    brin            TEXT NOT NULL,
    name            TEXT NOT NULL,
    education_type  TEXT NOT NULL DEFAULT 'po',
    street          TEXT,
    house_number    TEXT,
    postcode        TEXT NOT NULL,
    city            TEXT,
    gemeente        TEXT,
    province        TEXT,
    denomination    TEXT,
    latitude        REAL,
    longitude       REAL,
    rating          TEXT,
    rating_as_of    TEXT
);
CREATE INDEX IF NOT EXISTS idx_schools_lat ON schools(latitude);
CREATE INDEX IF NOT EXISTS idx_schools_pc ON schools(postcode);

-- Postcode -> coordinate cache. Postcodes do not move, so this is permanent
-- and makes a rebuild after a registry refresh nearly free.
CREATE TABLE IF NOT EXISTS postcode_geo (
    postcode    TEXT PRIMARY KEY,
    latitude    REAL NOT NULL,
    longitude   REAL NOT NULL,
    resolved_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS index_meta (
    key         TEXT PRIMARY KEY,
    value       TEXT
);
"""

@dataclass(frozen=True)
class IndexedSchool:
    vestigingscode: str
    brin: str
    name: str
    education_type: str
    postcode: str
    city: Optional[str]
    denomination: Optional[str]
    latitude: float
    longitude: float
    rating: Optional[str]
    rating_as_of: Optional[str]
    distance_m: int


class SchoolIndexMissing(Exception):
    """The index has not been built yet."""


class SchoolIndex:
    """Read-only radius queries over the cached registry."""

    def __init__(self, path: Path | str = DEFAULT_INDEX_PATH) -> None:
        self.path = Path(path)

    def exists(self) -> bool:
        return self.path.exists()

    def _connect(self) -> sqlite3.Connection:
        if not self.exists():
            raise SchoolIndexMissing(
                f"no school index at {self.path}; run scripts/build_school_index.py"
            )
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def nearby(
        self,
        latitude: float,
        longitude: float,
        *,
        radius_m: int = 2000,
        education_type: str = "po",
        limit: int = 25,
    ) -> list[IndexedSchool]:
        """Schools within ``radius_m``, nearest first.

        A latitude/longitude bounding box narrows the candidates in SQL, then
        the exact great-circle distance is computed on what survives — the box
        is a superset of the circle, so nothing near the edge is lost.
        """
        dlat = radius_m / 111_320.0
        dlon = radius_m / (111_320.0 * max(math.cos(math.radians(latitude)), 0.01))

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM schools
                 WHERE latitude IS NOT NULL
                   AND education_type = ?
                   AND latitude BETWEEN ? AND ?
                   AND longitude BETWEEN ? AND ?
                """,
                (education_type, latitude - dlat, latitude + dlat,
                 longitude - dlon, longitude + dlon),
            ).fetchall()

        found: list[IndexedSchool] = []
        for row in rows:
            distance = haversine_m(latitude, longitude, row["latitude"], row["longitude"])
            if distance > radius_m:
                continue
            found.append(IndexedSchool(
                vestigingscode=row["vestigingscode"], brin=row["brin"], name=row["name"],
                education_type=row["education_type"], postcode=row["postcode"],
                city=row["city"], denomination=row["denomination"],
                latitude=row["latitude"], longitude=row["longitude"],
                rating=row["rating"], rating_as_of=row["rating_as_of"],
                distance_m=int(round(distance)),
            ))
        found.sort(key=lambda s: s.distance_m)
        return found[:limit]

    def meta(self) -> dict[str, str]:
        with self._connect() as conn:
            return {r["key"]: r["value"] for r in conn.execute("SELECT * FROM index_meta")}

    def count(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM schools").fetchone()[0]
