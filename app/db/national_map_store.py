"""Storage for the national buurt-level map — a separate file, deliberately.

Two reasons this is not another table in the personal watch-loop database:

* **Different lifecycle.** The property store grows one row at a time as the
  watch loop finds listings. This is rebuilt wholesale, occasionally, by
  ``scripts/build_national_map.py`` — closer to ``data/schools.db`` than to
  ``data/woonagent.db``.
* **Different engine, on purpose.** Built with DuckDB's spatial extension
  (verified against this codebase's own known-good coordinate pairs before
  being trusted — see the build script), because that geometry work needs a
  real GEOS/PROJ implementation, not hand-rolled polygon maths. The FINISHED
  table, though, is plain columns — buurtcode, five scores, and a pre-computed
  GeoJSON string — so *reading* it needs nothing spatial at all. That is why
  this store never calls ``LOAD spatial``: only the build script does.

DuckDB's Python driver is synchronous. Every call here is expected to be
wrapped in ``asyncio.to_thread`` by the caller (see ``GET /national-map`` in
``app/main.py``) rather than taught to fake being async itself.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import duckdb

DEFAULT_DB_PATH = Path("data/national_map.duckdb")

SCHEMA = """
CREATE TABLE IF NOT EXISTS buurten (
    buurtcode          TEXT PRIMARY KEY,
    buurtnaam          TEXT,
    gemeentecode       TEXT,
    gemeentenaam       TEXT,
    rd_x               DOUBLE,
    rd_y               DOUBLE,
    lat                DOUBLE,
    lon                DOUBLE,
    geometry_geojson   TEXT,
    safety             DOUBLE,
    family             DOUBLE,
    education          DOUBLE,
    environment        DOUBLE,
    structural         DOUBLE,
    confidence         DOUBLE,
    coverage_pct       DOUBLE,
    computed_at        TIMESTAMP
);
"""

#: The five dimensions, in the fixed order every row and every query uses.
DIMENSIONS = ("safety", "family", "education", "environment", "structural")


class NationalMapStore:
    """One DuckDB file. Not async, not a connection pool — a batch script
    and an occasional read-only web request are the only two callers, and
    neither needs either."""

    def __init__(self, path: Path | str = DEFAULT_DB_PATH, *, read_only: bool = False) -> None:
        self.path = Path(path)
        if read_only and not self.path.exists():
            raise FileNotFoundError(f"no national map database at {self.path}")
        self.con = duckdb.connect(str(self.path), read_only=read_only)
        if not read_only:
            self.con.execute(SCHEMA)

    def close(self) -> None:
        self.con.close()

    # -- writing: the geometry pass -----------------------------------------

    def upsert_geometry(
        self,
        *,
        buurtcode: str,
        buurtnaam: Optional[str],
        gemeentecode: Optional[str],
        gemeentenaam: Optional[str],
        rd_x: float,
        rd_y: float,
        lat: float,
        lon: float,
        geometry_geojson: str,
    ) -> None:
        """Record one buurt's shape and centroid, before anything is scored.

        A plain INSERT ... ON CONFLICT rather than a separate exists-check:
        a re-run of the geometry pass (CBS republishes boundaries yearly)
        should just overwrite the shape and leave any already-computed scores
        alone, since scoring is the expensive half of this job.
        """
        self.con.execute(
            """
            INSERT INTO buurten (buurtcode, buurtnaam, gemeentecode, gemeentenaam,
                                 rd_x, rd_y, lat, lon, geometry_geojson)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (buurtcode) DO UPDATE SET
                buurtnaam = excluded.buurtnaam,
                gemeentecode = excluded.gemeentecode,
                gemeentenaam = excluded.gemeentenaam,
                rd_x = excluded.rd_x, rd_y = excluded.rd_y,
                lat = excluded.lat, lon = excluded.lon,
                geometry_geojson = excluded.geometry_geojson
            """,
            [buurtcode, buurtnaam, gemeentecode, gemeentenaam,
             rd_x, rd_y, lat, lon, geometry_geojson],
        )

    # -- writing: the scoring pass -------------------------------------------

    def save_score(
        self,
        buurtcode: str,
        *,
        safety: Optional[float], family: Optional[float], education: Optional[float],
        environment: Optional[float], structural: Optional[float],
        confidence: float, coverage_pct: float,
    ) -> None:
        self.con.execute(
            """
            UPDATE buurten SET
                safety = ?, family = ?, education = ?, environment = ?, structural = ?,
                confidence = ?, coverage_pct = ?, computed_at = now()
            WHERE buurtcode = ?
            """,
            [safety, family, education, environment, structural,
             confidence, coverage_pct, buurtcode],
        )

    def pending_buurten(self) -> list[dict[str, Any]]:
        """Buurten with a shape but no score yet — what a resumed build
        script still has to do. Ordering by buurtcode just keeps repeated
        runs' progress logs comparable; it carries no other meaning."""
        rows = self.con.execute(
            """
            SELECT buurtcode, buurtnaam, gemeentecode, gemeentenaam, rd_x, rd_y, lat, lon
              FROM buurten WHERE computed_at IS NULL ORDER BY buurtcode
            """
        ).fetchall()
        cols = ["buurtcode", "buurtnaam", "gemeentecode", "gemeentenaam",
                "rd_x", "rd_y", "lat", "lon"]
        return [dict(zip(cols, row)) for row in rows]

    def counts(self) -> dict[str, int]:
        total, scored = self.con.execute(
            "SELECT count(*), count(computed_at) FROM buurten"
        ).fetchone()
        return {"total": total, "scored": scored}

    # -- reading, for the API -------------------------------------------------

    def as_geojson(self) -> dict[str, Any]:
        """Every scored buurt as one FeatureCollection, ready to hand to a
        map library unmodified. Buurten with no score at all (a build in
        progress, or one whose enrichment failed outright) are left out
        rather than shown as a false zero."""
        rows = self.con.execute(
            f"""
            SELECT buurtcode, buurtnaam, gemeentecode, gemeentenaam,
                   {", ".join(DIMENSIONS)}, confidence, coverage_pct, geometry_geojson
              FROM buurten
             WHERE computed_at IS NOT NULL
            """
        ).fetchall()

        features = []
        for row in rows:
            (buurtcode, buurtnaam, gemeentecode, gemeentenaam,
             safety, family, education, environment, structural,
             confidence, coverage_pct, geometry_geojson) = row
            features.append({
                "type": "Feature",
                "geometry": json.loads(geometry_geojson),
                "properties": {
                    "buurtcode": buurtcode, "buurtnaam": buurtnaam,
                    "gemeentecode": gemeentecode, "gemeentenaam": gemeentenaam,
                    "safety": safety, "family": family, "education": education,
                    "environment": environment, "structural": structural,
                    "confidence": confidence, "coverage_pct": coverage_pct,
                },
            })
        total, scored = self.con.execute("SELECT count(*), count(computed_at) FROM buurten").fetchone()
        return {
            "type": "FeatureCollection", "features": features,
            # Not part of the GeoJSON spec proper, but RFC 7946 permits
            # foreign members and every consumer here (Leaflet included)
            # ignores keys it does not recognise — cheaper than a second
            # endpoint just to say how complete the map is.
            "total_buurten": total, "scored_buurten": scored,
        }
