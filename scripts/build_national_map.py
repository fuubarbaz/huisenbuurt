#!/usr/bin/env python3
"""Build the national buurt-level map: one score per CBS buurt, countrywide.

    python scripts/build_national_map.py [--limit N]

Two phases, and they can be re-run independently — the second is idempotent
per buurt, so an interrupted run picks up exactly where it left off.

**Phase 1 — geometry.** Every CBS buurt (~14,700 of them) is fetched in one
paginated crawl from PDOK's ``wijkenbuurten`` WFS, in its native Rijksdriehoek
projection. DuckDB's spatial extension — not hand-rolled polygon maths —
computes each buurt's centroid, reprojects its shape to WGS84, and simplifies
it for the browser. Verified against this codebase's own known-good
coordinate pair (Cruquiuskade 289, resolved earlier via PDOK Locatieserver)
before being trusted: ``ST_Transform(..., 'OGC:CRS84')`` reproduces it to
seven decimal places. ``'OGC:CRS84'``, not ``'EPSG:4326'``, is not a
stylistic choice — EPSG:4326 is officially latitude-first, and GeoJSON
(RFC 7946) requires longitude-first. Asking DuckDB for EPSG:4326 directly
silently hands back swapped coordinates; CRS84 is the same datum with the
axis order GeoJSON and every map library actually expect.

**Phase 2 — scoring.** Each buurt centroid is run through the exact same
``EnrichmentPipeline`` and ``ScoringEngine`` a real address uses — a buurt is
handed to them as a listing with no price, no construction year and no
street number, which every provider already tolerates for a real address
missing the same fields. Nothing about the scoring engine changes for this;
that is deliberate. The one place this is a real approximation: the
foundation-risk provider's polygon tie-break normally uses the listing's own
postcode, and a buurt has no single one — it falls back to whichever polygon
the query box happens to return, same as any address whose postcode does not
match. A national overview does not need address-level precision here.

Politeness is unchanged from every other path in this codebase: everything
goes through the shared, rate-limited ``HttpClient``, so PDOK, CBS and RIVM
see the same per-host budgets a normal watch-loop cycle respects — this
script just makes far more calls over a much longer run.

**Measured, not guessed. Two modes, two real numbers:**

* **Coarse (the default).** Noise drops to RIVM's own cumulative layer alone
  — which real address-scoring already PREFERS over summing the individual
  sources, so this costs zero scoring accuracy for noise specifically — and
  air drops to PM2.5 alone (a real tradeoff: full scoring blends NO2, PM2.5
  and PM10, and this keeps only the pollutant with the largest weight and
  health burden). Three RIVM calls per buurt instead of eleven. Measured live
  on a real 60-buurt batch: a uniform ~12 seconds per buurt at 20-way
  concurrency, ~1.8 buurten/second — **roughly 2.3 hours for the whole
  country.**
* **``--full``.** Every layer a real address gets. Measured live the same
  way: a uniform ~44 seconds per buurt, ~0.45/second — **roughly 9 hours.**

Both numbers come from the same underlying arithmetic: RIVM has no
measured-and-raised limit the way PDOK does (see rate_limit.py), so it falls
to the conservative open-data default of 5 requests/second, and the ceiling
is (5 ÷ RIVM-calls-per-buurt), not local concurrency — raising
``MAX_CONCURRENT_BUURTEN`` does not help either mode, it only changes how many
buurten queue at once for the same shared budget.

``--limit`` (with ``--skip-geometry`` on a second run) scores a small sample
first if you want to see it work, or start it and let it run — every buurt
scored is saved immediately, so an interrupted run resumes exactly where it
left off rather than losing progress. Coarse and full results live side by
side in the same database with no marker distinguishing them; re-running
``--full`` over already-coarse-scored buurten would require clearing
``computed_at`` for those rows first, since ``pending_buurten()`` only
returns buurten that have never been scored at all.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import duckdb  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.http_client import HttpClient  # noqa: E402
from app.core.logging import setup_logging  # noqa: E402
from app.db.national_map_store import DIMENSIONS, NationalMapStore  # noqa: E402
from app.models.property import GeoIdentity, ListingSource, PropertyListing  # noqa: E402
from app.services.enrichment import EnrichmentPipeline  # noqa: E402
from app.services.enrichment.providers.rivm_air import RIVMAirQualityProvider  # noqa: E402
from app.services.enrichment.providers.rivm_air import COARSE_LAYERS as COARSE_AIR_LAYERS  # noqa: E402
from app.services.enrichment.providers.rivm_noise import RIVMNoiseProvider  # noqa: E402
from app.services.enrichment.providers.rivm_noise import COARSE_LAYERS as COARSE_NOISE_LAYERS  # noqa: E402
from app.services.scoring import ScoringEngine  # noqa: E402

log = logging.getLogger("build_national_map")

WFS_URL = "https://service.pdok.nl/cbs/wijkenbuurten/2024/wfs/v1_0"
TYPE_NAME = "wijkenbuurten:buurten"
PAGE_SIZE = 1000

#: CBS's sentinel for "suppressed / not applicable" in this dataset — see the
#: "Buitenland" sample checked while building this: every numeric field on a
#: non-geographic buurt reads exactly this value.
CBS_SENTINEL = -99997

#: Simplification tolerance in degrees, applied after reprojecting to WGS84.
#: ~0.0001 deg is roughly 10m at Dutch latitudes — a national choropleth does
#: not need finer boundary detail than that, and it cuts the served payload
#: substantially over the untouched CBS boundaries.
SIMPLIFY_TOLERANCE_DEG = 0.0001

#: Outer bound on buurten scored concurrently. This does not police any host's
#: request rate — the shared HttpClient's per-host token buckets already do
#: that regardless of how much is in flight here — it only caps how much local
#: work (open connections, memory) this process carries at once.
MAX_CONCURRENT_BUURTEN = 20


async def fetch_all_buurten_rd(http: HttpClient) -> list[dict[str, Any]]:
    """Every buurt, native Rijksdriehoek, geometry included.

    Paginated with ``count``/``startIndex`` — both verified live to work
    correctly (unlike ``resultType=hits``, which this service ignores when
    JSON output is requested, so the total feature count is discovered by
    paging to an empty page rather than asked for upfront).
    """
    features: list[dict[str, Any]] = []
    start = 0
    while True:
        payload = await http.get_json(
            WFS_URL,
            params={
                "service": "WFS", "version": "2.0.0", "request": "GetFeature",
                "typeName": TYPE_NAME, "outputFormat": "application/json",
                "count": PAGE_SIZE, "startIndex": start,
            },
        )
        page = payload.get("features") or []
        if not page:
            break
        features.extend(page)
        log.info("fetched %d buurten so far", len(features))
        start += PAGE_SIZE
    return features


def load_geometry(store: NationalMapStore, features: list[dict[str, Any]]) -> None:
    """Phase 1: centroid + reprojected, simplified shape for every buurt.

    A short-lived DuckDB connection with the spatial extension loaded, kept
    separate from the store's own connection: the store's schema is
    deliberately spatial-extension-free (see its module docstring), and this
    is the one place in the whole feature that needs GEOS/PROJ at all.
    """
    geo = duckdb.connect()
    geo.execute("INSTALL spatial")
    geo.execute("LOAD spatial")

    skipped = 0
    for feature in features:
        props = feature.get("properties") or {}
        buurtcode = props.get("buurtcode")
        geometry = feature.get("geometry")
        if not buurtcode or not geometry:
            skipped += 1
            continue

        row = geo.execute(
            """
            WITH g AS (SELECT ST_GeomFromGeoJSON(?) AS geom),
                 wgs AS (SELECT ST_Transform(geom, 'EPSG:28992', 'OGC:CRS84') AS geom FROM g)
            SELECT
                ST_X(ST_Centroid(g.geom)), ST_Y(ST_Centroid(g.geom)),
                ST_X(ST_Centroid(wgs.geom)), ST_Y(ST_Centroid(wgs.geom)),
                ST_AsGeoJSON(ST_SimplifyPreserveTopology(wgs.geom, ?))
            FROM g, wgs
            """,
            [json.dumps(geometry), SIMPLIFY_TOLERANCE_DEG],
        ).fetchone()
        rd_x, rd_y, lon, lat, simplified_geojson = row

        def clean(value: Any) -> Optional[str]:
            return str(value) if value not in (None, CBS_SENTINEL, str(CBS_SENTINEL)) else None

        store.upsert_geometry(
            buurtcode=buurtcode,
            buurtnaam=clean(props.get("buurtnaam")),
            gemeentecode=clean(props.get("gemeentecode")),
            gemeentenaam=clean(props.get("gemeentenaam")),
            rd_x=rd_x, rd_y=rd_y, lat=lat, lon=lon,
            geometry_geojson=simplified_geojson,
        )

    geo.close()
    if skipped:
        log.warning("skipped %d feature(s) with no buurtcode or geometry", skipped)


def _placeholder_postcode(meest_voorkomende: Any) -> str:
    """A syntactically valid PC6 for the synthetic listing.

    Used only as the foundation-risk provider's polygon tie-break (see the
    module docstring) — never for geocoding, since this buurt's GeoIdentity is
    supplied directly and no address is ever resolved through PDOK for it. If
    it does not exactly match a real PC6, the provider's own documented
    fallback (the query box's first result) applies, same as it would for any
    real address whose stated postcode misses.
    """
    text = str(meest_voorkomende or "")
    pc4 = text if text.isdigit() and len(text) == 4 else "1000"
    return f"{pc4}AA"


def _synthetic_listing_and_geo(buurt: dict[str, Any]) -> tuple[PropertyListing, GeoIdentity]:
    buurtcode = buurt["buurtcode"]
    label = ", ".join(filter(None, [buurt.get("buurtnaam"), buurt.get("gemeentenaam")])) \
        or buurtcode

    listing = PropertyListing(
        property_id=f"buurt-{buurtcode}",
        source=ListingSource.MANUAL,
        url="https://example.invalid/national-map",
        address=label,
        postal_code=_placeholder_postcode(buurt.get("meest_voorkomende_postcode")),
        house_number="1",
    )
    geo = GeoIdentity(
        latitude=buurt["lat"], longitude=buurt["lon"],
        rd_x=buurt["rd_x"], rd_y=buurt["rd_y"],
        buurtcode=buurtcode, buurtnaam=buurt.get("buurtnaam"),
        gemeentecode=buurt.get("gemeentecode"), gemeentenaam=buurt.get("gemeentenaam"),
        matched_address=label,
    )
    return listing, geo


def _build_pipeline(http: HttpClient, coarse: bool) -> EnrichmentPipeline:
    """The real pipeline, or a coarser one for a national overview.

    Coarse mode swaps only the noise and air providers for versions that
    fetch far fewer RIVM layers — see COARSE_LAYERS in each provider module
    for exactly what that gives up. Crime, demographics, education and
    foundation risk are untouched either way: they were never the bottleneck
    (see the module docstring's measured throughput numbers), and there is no
    equivalent coarser mode for them to opt into.
    """
    if not coarse:
        return EnrichmentPipeline(http)

    providers = {key: cls(http) for key, cls in EnrichmentPipeline.PROVIDER_MAP.items()}
    providers["noise"] = RIVMNoiseProvider(http, layers=COARSE_NOISE_LAYERS)
    providers["air"] = RIVMAirQualityProvider(http, layers=COARSE_AIR_LAYERS)
    return EnrichmentPipeline(http, providers=providers)


async def score_buurten(
    store: NationalMapStore, http: HttpClient, buurten: list[dict[str, Any]], *, coarse: bool,
) -> None:
    """Phase 2: enrich and score every buurt that has geometry but no score."""
    pipeline = _build_pipeline(http, coarse)
    scorer = ScoringEngine()
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_BUURTEN)
    done = 0
    total = len(buurten)
    started = datetime.now(timezone.utc)

    async def score_one(buurt: dict[str, Any]) -> None:
        nonlocal done
        async with semaphore:
            listing, geo = _synthetic_listing_and_geo(buurt)
            try:
                bundle = await pipeline.enrich_one(listing, geo=geo)
                score = scorer.score(bundle)
            except Exception as exc:  # noqa: BLE001 — one bad buurt must not stop the run
                log.warning("scoring failed for %s: %s", buurt["buurtcode"], exc)
                return

            by_dimension = {d.dimension.value: d.score for d in score.dimensions}
            store.save_score(
                buurt["buurtcode"],
                safety=by_dimension.get("physical_safety"),
                family=by_dimension.get("peer_family_concentration"),
                education=by_dimension.get("education_quality_access"),
                environment=by_dimension.get("environmental_health"),
                structural=by_dimension.get("structural_security"),
                confidence=score.confidence, coverage_pct=score.data_coverage_pct,
            )
            done += 1
            if done % 100 == 0 or done == total:
                elapsed = (datetime.now(timezone.utc) - started).total_seconds()
                log.info("%d/%d buurten scored (%.0fs elapsed, ~%.1f/s)",
                         done, total, elapsed, done / max(elapsed, 1))

    await asyncio.gather(*(score_one(b) for b in buurten))


async def main(limit: Optional[int], skip_geometry: bool, coarse: bool) -> None:
    setup_logging()
    store = NationalMapStore(Path(settings.national_map_db_path))
    try:
        async with HttpClient() as http:
            if skip_geometry:
                log.info("phase 1: skipped (--skip-geometry), reusing what is already stored")
            else:
                log.info("phase 1: fetching buurt geometry from PDOK")
                features = await fetch_all_buurten_rd(http)
                log.info("loaded %d buurten; computing centroids and simplified shapes", len(features))
                load_geometry(store, features)

            pending = store.pending_buurten()
            if limit:
                pending = pending[:limit]
            log.info("phase 2: scoring %d buurt(s) in %s mode (%s already done)",
                     len(pending), "coarse" if coarse else "full", store.counts())

            await score_buurten(store, http, pending, coarse=coarse)
    finally:
        counts = store.counts()
        store.close()
        log.info("done: %d/%d buurten scored", counts["scored"], counts["total"])


def cli() -> None:
    parser = argparse.ArgumentParser(description="Build the national buurt-level map.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Score only this many buurten (for a quick trial run).")
    parser.add_argument("--skip-geometry", action="store_true",
                        help="Reuse geometry already in the database; only run phase 2.")
    parser.add_argument("--full", action="store_true",
                        help=("Fetch every noise/air sub-layer per buurt, same as a real "
                              "address (~9h for the whole country). Default is the coarse, "
                              "~4x faster mode described in the module docstring."))
    args = parser.parse_args()
    asyncio.run(main(args.limit, args.skip_geometry, coarse=not args.full))


if __name__ == "__main__":
    cli()
