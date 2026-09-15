"""Persistence boundary. Everything above this layer speaks Pydantic models,
never SQL — which is what lets the same services back a FastAPI app later.

Two guards live here, and they are separate on purpose:

* **Duplicate processing** — :meth:`upsert_listing` returns ``True`` only for a
  genuinely new row. A listing seen again just has its ``last_seen_at`` bumped,
  and the orchestrator skips it without spending a geocode or six API calls.
* **Duplicate alerting** — the ``notifications`` table. A property can be
  re-scored (weights change, a provider recovers) without the user being
  pinged about the same house twice.

Conflating the two would mean either re-alerting on every poll or never
re-scoring anything.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Optional, Sequence

import aiosqlite

from app.core.geometry import bounding_box, haversine_m
from app.models.enrichment import EnrichmentBundle
from app.models.property import GeoIdentity, ListingSource, PropertyListing
from app.models.score import HolisticScore

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

VALID_STATUSES = {"new", "enriched", "scored", "notified", "failed", "ignored"}


def sqlite_path(database_url: str) -> str:
    """Extract a filesystem path from a SQLAlchemy-style URL.

    The config carries ``sqlite+aiosqlite:///./data/woonagent.db`` so that
    swapping in SQLAlchemy later needs no config change; this layer speaks raw
    aiosqlite and only wants the path.
    """
    stripped = re.sub(r"^sqlite(\+\w+)?:///", "", database_url.strip())
    return stripped or ":memory:"


class PropertyRepository:
    """Async SQLite repository.

    Holds one connection guarded by SQLite's own locking. Call
    :meth:`init_schema` once at startup and :meth:`close` at shutdown.
    """

    def __init__(self, database_url: str) -> None:
        self.path = sqlite_path(database_url)
        self._conn: Optional[aiosqlite.Connection] = None

    # -- lifecycle ---------------------------------------------------------

    async def connect(self) -> aiosqlite.Connection:
        if self._conn is None:
            if self.path != ":memory:":
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = await aiosqlite.connect(self.path)
            self._conn.row_factory = aiosqlite.Row
            await self._conn.execute("PRAGMA foreign_keys = ON")
        return self._conn

    async def init_schema(self) -> None:
        conn = await self.connect()
        await conn.executescript(SCHEMA_PATH.read_text())
        await conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self) -> "PropertyRepository":
        await self.init_schema()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    # -- listings ----------------------------------------------------------

    async def upsert_listing(self, listing: PropertyListing) -> bool:
        """Store a listing. Returns ``True`` only if it was not already known.

        Implemented as INSERT-OR-IGNORE followed by a conditional UPDATE rather
        than a single upsert, because SQLite's ``ON CONFLICT DO UPDATE`` cannot
        report whether it inserted or updated — and that distinction is the
        entire point of the call.
        """
        conn = await self.connect()
        cursor = await conn.execute(
            """
            INSERT OR IGNORE INTO properties (
                property_id, source, url, address, postal_code, house_number,
                house_number_addition, city, price_eur, living_area_m2,
                plot_area_m2, rooms, construction_year, listed_at, status
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'new')
            """,
            (
                listing.property_id, listing.source.value, listing.url, listing.address,
                listing.postal_code, listing.house_number, listing.house_number_addition,
                listing.city, listing.price_eur, listing.living_area_m2,
                listing.plot_area_m2, listing.rooms, listing.construction_year,
                listing.listed_at.isoformat() if listing.listed_at else None,
            ),
        )
        is_new = cursor.rowcount == 1

        if not is_new:
            # Known property: refresh what can legitimately change, and record
            # that it is still on the market.
            await conn.execute(
                """
                UPDATE properties
                   SET last_seen_at = datetime('now'),
                       price_eur = COALESCE(?, price_eur),
                       url = ?
                 WHERE property_id = ?
                """,
                (listing.price_eur, listing.url, listing.property_id),
            )
        await conn.commit()
        return is_new

    async def get_listing(self, property_id: str) -> Optional[PropertyListing]:
        conn = await self.connect()
        async with conn.execute(
            "SELECT * FROM properties WHERE property_id = ?", (property_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return _to_listing(row) if row else None

    async def list_by_status(self, status: str, limit: int = 50) -> Sequence[PropertyListing]:
        conn = await self.connect()
        async with conn.execute(
            "SELECT * FROM properties WHERE status = ? ORDER BY first_seen_at DESC LIMIT ?",
            (status, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [_to_listing(r) for r in rows]

    async def set_status(self, property_id: str, status: str) -> None:
        if status not in VALID_STATUSES:
            raise ValueError(f"unknown status {status!r}; expected one of {sorted(VALID_STATUSES)}")
        conn = await self.connect()
        await conn.execute(
            "UPDATE properties SET status = ? WHERE property_id = ?", (status, property_id)
        )
        await conn.commit()

    # -- geocoding ---------------------------------------------------------

    async def save_geo(self, property_id: str, geo: GeoIdentity) -> None:
        conn = await self.connect()
        await conn.execute(
            """
            INSERT OR REPLACE INTO geo_identity (
                property_id, latitude, longitude, rd_x, rd_y, buurtcode, buurtnaam,
                wijkcode, gemeentecode, gemeentenaam, provincie, matched_address
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                property_id, geo.latitude, geo.longitude, geo.rd_x, geo.rd_y,
                geo.buurtcode, geo.buurtnaam, geo.wijkcode, geo.gemeentecode,
                geo.gemeentenaam, geo.provincie, geo.matched_address,
            ),
        )
        await conn.commit()

    async def cached_geo(self, property_id: str) -> Optional[GeoIdentity]:
        """A resolved address, if we have one. Coordinates never change."""
        conn = await self.connect()
        async with conn.execute(
            "SELECT * FROM geo_identity WHERE property_id = ?", (property_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return GeoIdentity(
            latitude=row["latitude"], longitude=row["longitude"],
            rd_x=row["rd_x"], rd_y=row["rd_y"],
            buurtcode=row["buurtcode"], buurtnaam=row["buurtnaam"],
            wijkcode=row["wijkcode"], gemeentecode=row["gemeentecode"],
            gemeentenaam=row["gemeentenaam"], provincie=row["provincie"],
            matched_address=row["matched_address"],
        )

    # -- enrichment and scores ---------------------------------------------

    async def save_enrichment(self, bundle: EnrichmentBundle) -> None:
        conn = await self.connect()
        await conn.execute(
            """
            INSERT OR REPLACE INTO enrichment (property_id, bundle_json, coverage_pct, enriched_at)
            VALUES (?,?,?, datetime('now'))
            """,
            (bundle.property_id, bundle.model_dump_json(), bundle.coverage_pct),
        )
        # The geocode is worth caching separately so a re-run skips PDOK.
        if bundle.geo is not None:
            await self.save_geo(bundle.property_id, bundle.geo)
        await conn.commit()

    async def get_enrichment(self, property_id: str) -> Optional[EnrichmentBundle]:
        conn = await self.connect()
        async with conn.execute(
            "SELECT bundle_json FROM enrichment WHERE property_id = ?", (property_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return EnrichmentBundle.model_validate_json(row["bundle_json"]) if row else None

    async def save_score(self, score: HolisticScore) -> None:
        conn = await self.connect()
        await conn.execute(
            """
            INSERT OR REPLACE INTO scores
                (property_id, total_score, confidence, dimensions_json, risk_flags_json, scored_at)
            VALUES (?,?,?,?,?, datetime('now'))
            """,
            (
                score.property_id, score.total_score, score.confidence,
                json.dumps([d.model_dump(mode="json") for d in score.dimensions]),
                json.dumps([f.model_dump(mode="json") for f in score.risk_flags]),
            ),
        )
        await conn.commit()

    async def pending_notification(
        self, min_score: float, limit: int = 25, channel: str = "telegram"
    ) -> list[str]:
        """Scored properties above the bar that have never been delivered.

        Exists because the duplicate-processing guard would otherwise strand
        them: a property seen on an earlier cycle is skipped before the notify
        step is reached, so a transient Telegram outage would lose the alert
        permanently. This is the query that lets a later cycle pick it up.
        """
        conn = await self.connect()
        async with conn.execute(
            """
            SELECT s.property_id
              FROM scores s
              LEFT JOIN notifications n
                     ON n.property_id = s.property_id
                    AND n.channel = ?
                    AND n.success = 1
             WHERE s.total_score >= ?
               AND n.property_id IS NULL
             ORDER BY s.total_score DESC
             LIMIT ?
            """,
            (channel, min_score, limit),
        ) as cursor:
            return [r["property_id"] for r in await cursor.fetchall()]

    async def top_scored(self, limit: int = 20, min_score: float = 0.0) -> list[dict[str, Any]]:
        """Best-scoring properties seen so far — the backing query for a future
        'shortlist' endpoint."""
        conn = await self.connect()
        async with conn.execute(
            """
            SELECT p.property_id, p.address, p.url, p.price_eur, p.living_area_m2,
                   s.total_score, s.confidence, s.scored_at
              FROM scores s JOIN properties p USING (property_id)
             WHERE s.total_score >= ?
             ORDER BY s.total_score DESC LIMIT ?
            """,
            (min_score, limit),
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]

    async def nearby(
        self,
        *,
        latitude: float,
        longitude: float,
        radius_m: float = 2000.0,
        limit: int = 10,
        exclude_property_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Properties already in the store, nearest first.

        The comparables a buyer actually wants are recent *sales*, and those
        are Kadaster's: licensed, not open. CBS publishes sale prices only down
        to the municipality (745 regions, none finer than GM), and Funda is off
        limits by the decision in ``app/scrapers/funda.py``. So this answers the
        question the open data can answer — "what else near here has this agent
        seen, and what was it asking" — and the panel says so rather than
        implying these are sold prices.

        Two-stage on purpose: an indexed lat/lon box in SQL, then the exact
        haversine in Python. SQLite only has trigonometric functions when built
        with SQLITE_ENABLE_MATH_FUNCTIONS, so a distance computed in the query
        would work on this machine and fail on another.
        """
        lat_min, lat_max, lon_min, lon_max = bounding_box(latitude, longitude, radius_m)
        conn = await self.connect()
        async with conn.execute(
            """
            SELECT p.property_id, p.address, p.url, p.price_eur, p.living_area_m2,
                   p.construction_year, p.first_seen_at, p.status,
                   g.latitude, g.longitude, g.buurtnaam,
                   s.total_score
              FROM geo_identity g
              JOIN properties p USING (property_id)
              LEFT JOIN scores s USING (property_id)
             WHERE g.latitude BETWEEN ? AND ?
               AND g.longitude BETWEEN ? AND ?
               AND p.property_id IS NOT ?
            """,
            (lat_min, lat_max, lon_min, lon_max, exclude_property_id),
        ) as cursor:
            rows = [dict(r) for r in await cursor.fetchall()]

        out: list[dict[str, Any]] = []
        for row in rows:
            distance = haversine_m(latitude, longitude, row["latitude"], row["longitude"])
            if distance > radius_m:      # the box is a superset of the circle
                continue
            row["distance_m"] = round(distance)
            price, area = row.get("price_eur"), row.get("living_area_m2")
            row["price_per_m2"] = round(price / area) if price and area else None
            out.append(row)

        out.sort(key=lambda r: r["distance_m"])
        return out[:limit]

    # -- notification guard ------------------------------------------------

    async def is_notified(self, property_id: str, channel: str = "telegram") -> bool:
        """Whether the user has already been told about this property.

        Only a successful send counts: a failed delivery must not silence the
        retry on the next cycle.
        """
        conn = await self.connect()
        async with conn.execute(
            "SELECT 1 FROM notifications WHERE property_id = ? AND channel = ? AND success = 1",
            (property_id, channel),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def mark_notified(
        self, property_id: str, channel: str = "telegram", success: bool = True
    ) -> None:
        conn = await self.connect()
        await conn.execute(
            """
            INSERT INTO notifications (property_id, channel, sent_at, success)
            VALUES (?,?, datetime('now'), ?)
            ON CONFLICT(property_id, channel) DO UPDATE
               SET sent_at = datetime('now'), success = excluded.success
            """,
            (property_id, channel, int(success)),
        )
        await conn.commit()

    # -- area-level cache --------------------------------------------------

    async def cache_get(self, provider: str, area_key: str) -> Optional[dict[str, Any]]:
        """Area-keyed payload cache: ten listings in one buurt cost one call."""
        conn = await self.connect()
        async with conn.execute(
            """
            SELECT payload_json FROM enrichment_cache
             WHERE provider = ? AND area_key = ?
               AND (expires_at IS NULL OR expires_at > datetime('now'))
            """,
            (provider, area_key),
        ) as cursor:
            row = await cursor.fetchone()
        return json.loads(row["payload_json"]) if row else None

    async def cache_put(
        self, provider: str, area_key: str, payload: dict[str, Any], ttl_days: int | None = 30
    ) -> None:
        conn = await self.connect()
        # "+{n} days" breaks for a negative n: SQLite reads "+-1 days" as an
        # invalid modifier, returns NULL, and the row then never expires.
        # The sign has to come from the number itself.
        expiry = f"{ttl_days:+d} days" if ttl_days else None
        await conn.execute(
            """
            INSERT OR REPLACE INTO enrichment_cache
                (provider, area_key, payload_json, fetched_at, expires_at)
            VALUES (?,?,?, datetime('now'), CASE WHEN ? IS NULL THEN NULL
                                                 ELSE datetime('now', ?) END)
            """,
            (provider, area_key, json.dumps(payload), expiry, expiry),
        )
        await conn.commit()

    # -- housekeeping ------------------------------------------------------

    async def stats(self) -> dict[str, int]:
        conn = await self.connect()
        out: dict[str, int] = {}
        for label, query in [
            ("properties", "SELECT COUNT(*) FROM properties"),
            ("enriched", "SELECT COUNT(*) FROM enrichment"),
            ("scored", "SELECT COUNT(*) FROM scores"),
            ("notified", "SELECT COUNT(*) FROM notifications WHERE success = 1"),
        ]:
            async with conn.execute(query) as cursor:
                out[label] = (await cursor.fetchone())[0]
        return out


def _to_listing(row: aiosqlite.Row) -> PropertyListing:
    return PropertyListing(
        property_id=row["property_id"],
        source=ListingSource(row["source"]),
        url=row["url"],
        address=row["address"],
        postal_code=row["postal_code"],
        house_number=row["house_number"],
        house_number_addition=row["house_number_addition"],
        city=row["city"],
        price_eur=row["price_eur"],
        living_area_m2=row["living_area_m2"],
        plot_area_m2=row["plot_area_m2"],
        rooms=row["rooms"],
        construction_year=row["construction_year"],
        listed_at=row["listed_at"],
    )
