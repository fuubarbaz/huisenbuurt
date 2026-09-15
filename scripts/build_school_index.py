#!/usr/bin/env python3
"""Build the local school index from DUO open data.

    python scripts/build_school_index.py [--limit N] [--radius-only]

Downloads the primary-school registry and the inspection verdicts, geocodes
every school postcode through PDOK, and writes data/schools.db.

Takes roughly ten minutes on a cold run (6,096 schools at ~10 geocodes/sec).
Re-runs are far quicker: postcode coordinates are cached permanently in the
same database, because postcodes do not move.

Note on the CKAN metadata: the verdict resource URL published by the API points
at an internal DUO host that does not resolve publicly. It has to be rewritten
onto onderwijsdata.duo.nl — see PUBLIC_HOST below.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import io
import logging
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.http_client import HttpClient  # noqa: E402
from app.core.logging import setup_logging  # noqa: E402
from app.services.enrichment.school_index import DEFAULT_INDEX_PATH, SCHEMA  # noqa: E402
from app.services.geo.pdok_locatieserver import GeocodeError, PDOKLocatieserver  # noqa: E402

log = logging.getLogger("build_school_index")

VESTIGINGEN_CSV = (
    "https://onderwijsdata.duo.nl/dataset/786f12ea-6224-42fd-ab72-de4d7d879535"
    "/resource/dcc9c9a5-6d01-410b-967f-810557588ba4/download/vestigingenbo.csv"
)
OORDELEN_CSV = (
    "https://onderwijsdata.duo.nl/dataset/31da72f2-2858-4bc3-848e-dfe4875ba669"
    "/resource/b48d6835-0534-4008-82c8-1754b9080113/download/oordeel_po_so_vo.csv"
)
PUBLIC_HOST = "https://onderwijsdata.duo.nl"

GEOCODE_CONCURRENCY = 10


def read_csv(text: str) -> list[dict[str, str]]:
    delimiter = ";" if text.count(";") > text.count(",") else ","
    return list(csv.DictReader(io.StringIO(text), delimiter=delimiter))


def clean_pc(value: str | None) -> str:
    return (value or "").replace(" ", "").upper()


async def fetch_schools(http: HttpClient) -> list[dict]:
    rows = read_csv(await http.get_text(VESTIGINGEN_CSV))
    schools = []
    for r in rows:
        postcode = clean_pc(r.get("POSTCODE"))
        if len(postcode) != 6:
            continue
        schools.append({
            "vestigingscode": (r.get("VESTIGINGSCODE") or "").strip(),
            "brin": (r.get("INSTELLINGSCODE") or "").strip(),
            "name": (r.get("VESTIGINGSNAAM") or "").strip(),
            "education_type": "po",
            "street": (r.get("STRAATNAAM") or "").strip(),
            "house_number": (r.get("HUISNUMMER-TOEVOEGING") or "").strip(),
            "postcode": postcode,
            "city": (r.get("PLAATSNAAM") or "").strip().title(),
            "gemeente": (r.get("GEMEENTENAAM") or "").strip().title(),
            "province": (r.get("PROVINCIE") or "").strip(),
            "denomination": (r.get("DENOMINATIE") or "").strip(),
        })
    log.info("registry: %d primary schools with a usable postcode", len(schools))
    return schools


async def fetch_ratings(http: HttpClient) -> tuple[dict[str, dict], str | None]:
    """Latest inspection verdict per BRIN, plus the snapshot date."""
    rows = read_csv(await http.get_text(OORDELEN_CSV))
    ratings: dict[str, dict] = {}
    as_of = None
    for r in rows:
        if r.get("Sector") != "PO":
            continue
        brin = (r.get("BRIN") or "").strip()
        verdict = (r.get("EindoordeelKwaliteit") or "").strip()
        if not brin or not verdict:
            continue
        as_of = as_of or (r.get("Peildatum") or "").strip() or None
        # Keep the most informative verdict when a BRIN appears more than once.
        if brin in ratings and verdict in ("Geen oordeel", "Zonder actueel oordeel"):
            continue
        ratings[brin] = {"rating": verdict, "as_of": (r.get("Peildatum") or "").strip()}
    log.info("verdicts: %d schools rated, snapshot %s", len(ratings), as_of)
    return ratings, as_of


async def geocode_postcodes(
    http: HttpClient, conn: sqlite3.Connection, postcodes: set[str]
) -> None:
    """Resolve any postcode not already cached, respecting PDOK's rate limits."""
    cached = {r[0] for r in conn.execute("SELECT postcode FROM postcode_geo")}
    todo = sorted(postcodes - cached)
    log.info("geocoding: %d cached, %d to resolve", len(cached), len(todo))
    if not todo:
        return

    geocoder = PDOKLocatieserver(http)
    gate = asyncio.Semaphore(GEOCODE_CONCURRENCY)
    done = 0

    async def one(postcode: str):
        nonlocal done
        async with gate:
            try:
                geo = await geocoder.resolve(postal_code=postcode, house_number="1")
            except (GeocodeError, Exception) as exc:  # noqa: BLE001
                log.debug("geocode failed for %s: %s", postcode, exc)
                return None
            finally:
                done += 1
                if done % 500 == 0:
                    log.info("  geocoded %d/%d", done, len(todo))
            return (postcode, geo.latitude, geo.longitude)

    for start in range(0, len(todo), 500):
        chunk = todo[start:start + 500]
        results = [r for r in await asyncio.gather(*(one(p) for p in chunk)) if r]
        conn.executemany(
            "INSERT OR REPLACE INTO postcode_geo (postcode, latitude, longitude) VALUES (?,?,?)",
            results,
        )
        conn.commit()


async def main(limit: int | None) -> None:
    setup_logging()
    path = Path(DEFAULT_INDEX_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)

    async with HttpClient() as http:
        schools, (ratings, as_of) = await asyncio.gather(
            fetch_schools(http), fetch_ratings(http)
        )
        if limit:
            schools = schools[:limit]
        await geocode_postcodes(http, conn, {s["postcode"] for s in schools})

    coords = {r[0]: (r[1], r[2]) for r in
              conn.execute("SELECT postcode, latitude, longitude FROM postcode_geo")}

    written = 0
    for s in schools:
        lat_lon = coords.get(s["postcode"])
        verdict = ratings.get(s["brin"], {})
        conn.execute(
            """INSERT OR REPLACE INTO schools
               (vestigingscode, brin, name, education_type, street, house_number,
                postcode, city, gemeente, province, denomination,
                latitude, longitude, rating, rating_as_of)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (s["vestigingscode"], s["brin"], s["name"], s["education_type"],
             s["street"], s["house_number"], s["postcode"], s["city"],
             s["gemeente"], s["province"], s["denomination"],
             lat_lon[0] if lat_lon else None, lat_lon[1] if lat_lon else None,
             verdict.get("rating"), verdict.get("as_of")),
        )
        written += 1

    conn.executemany(
        "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?,?)",
        [("schools", str(written)), ("ratings_as_of", as_of or ""),
         ("source", "DUO open onderwijsdata")],
    )
    conn.commit()

    placed = conn.execute("SELECT COUNT(*) FROM schools WHERE latitude IS NOT NULL").fetchone()[0]
    rated = conn.execute("SELECT COUNT(*) FROM schools WHERE rating IS NOT NULL").fetchone()[0]
    log.info("wrote %d schools (%d geocoded, %d rated) to %s", written, placed, rated, path)
    conn.close()


def cli() -> None:
    """Console entry point: ``woonagent-build-index``."""
    parser = argparse.ArgumentParser(description="Build the local DUO school index.")
    parser.add_argument("--limit", type=int, default=None, help="Only index the first N schools.")
    asyncio.run(main(parser.parse_args().limit))


if __name__ == "__main__":
    cli()
