#!/usr/bin/env python3
"""Extract indicative renovation costs from Verbeterjehuis into a local table.

    python scripts/build_renovation_costs.py

Verbeterjehuis.nl is run by Milieu Centraal, a public-interest foundation, and
its robots.txt permits crawling outright (``User-agent: * / Allow: /``). Each
measure page publishes a cost table broken down by house type — investment,
subsidy, gas saved, money saved per year.

This runs **once**, writing ``data/renovation_costs.json``. The estimator then
reads that file, so scoring a property never touches their site. Their figures
change slowly; re-run when you want them refreshed.

The numbers stay attributed. They are Milieu Centraal's national indicative
figures, not a quote, and the estimator says so wherever it shows them.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.http_client import HttpClient  # noqa: E402
from app.core.logging import setup_logging  # noqa: E402
from app.core.robots import RobotsGate  # noqa: E402

log = logging.getLogger("build_renovation_costs")

SITEMAP = "https://www.verbeterjehuis.nl/sitemap.xml"
OUTPUT = Path("data/renovation_costs.json")

#: Only measure pages. The sitemap is dominated by ~700 subsidy and loan
#: entries under /energiesubsidiewijzer/; the measures themselves live under a
#: handful of /eigen-huis/ categories. Pages without a cost table are skipped
#: by the parser anyway, so this filter only has to be roughly right.
MEASURE_CATEGORIES = (
    "isoleren-ventileren-en-zon-weren",
    "verwarmen-en-koelen",
    "stroom-opwekken-en-gebruiken",
    "zelf-je-huis-isoleren",
    "verbeteropties",
)
MEASURE_URL_RE = re.compile(
    r"/(eigen-huis|huis-met-vve)/(" + "|".join(MEASURE_CATEGORIES) + r")/[a-z0-9-]+$"
)

LOC_RE = re.compile(r"<loc>([^<]+)</loc>")
TABLE_RE = re.compile(r"<table[^>]*>(.*?)</table>", re.S)
ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
CELL_RE = re.compile(r"<t[hd][^>]*>(.*?)</t[hd]>", re.S)
TAG_RE = re.compile(r"<[^>]+>")
TITLE_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S)

#: The house types the tables are broken down by.
HOUSE_TYPES = {
    "tussenwoning": "terraced",
    "hoekwoning": "end_terrace",
    "2-onder-1-kap": "semi_detached",
    "vrijstaande woning": "detached",
    "appartement": "apartment",
    "portiekwoning": "apartment",
    "galerijwoning": "apartment",
}


def clean(html: str) -> str:
    return re.sub(r"\s+", " ", TAG_RE.sub("", html)).strip()


def euros(text: str) -> int | None:
    """'€ 2.150' -> 2150. Dutch thousands separators are dots."""
    match = re.search(r"€\s*([\d.]+)", text)
    if not match:
        return None
    digits = match.group(1).replace(".", "")
    return int(digits) if digits.isdigit() else None


def number(text: str) -> int | None:
    match = re.search(r"([\d.]+)\s*m3", text)
    if not match:
        return None
    digits = match.group(1).replace(".", "")
    return int(digits) if digits.isdigit() else None


def parse_measure(url: str, html: str) -> dict | None:
    """Pull the house-type cost table out of one measure page."""
    title_match = TITLE_RE.search(html)
    title = clean(title_match.group(1)) if title_match else url.rsplit("/", 1)[-1]

    for table_html in TABLE_RE.findall(html):
        rows = [
            [clean(c) for c in CELL_RE.findall(row)]
            for row in ROW_RE.findall(table_html)
        ]
        rows = [r for r in rows if r]
        if not rows or not any("oning" in " ".join(r).lower() for r in rows):
            continue

        header = [h.lower() for h in rows[0]]
        if not any("kosten" in h for h in header):
            continue

        by_type: dict[str, dict] = {}
        for row in rows[1:]:
            key = HOUSE_TYPES.get(row[0].strip().lower())
            if not key or len(row) < 2:
                continue
            entry = {"cost_eur": euros(row[1])}
            if len(row) > 2:
                entry["subsidy_eur"] = euros(row[2])
            if len(row) > 3:
                entry["gas_saved_m3"] = number(row[3])
            if len(row) > 4:
                entry["saving_eur_year"] = euros(row[4])
            if entry["cost_eur"] is not None:
                by_type[key] = entry

        if by_type:
            return {
                "measure": url.rsplit("/", 1)[-1],
                "title": title,
                "url": url,
                "category": url.split("/eigen-huis/")[-1].split("/")[0],
                "by_house_type": by_type,
            }
    return None


async def main(limit: int | None) -> None:
    setup_logging()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    async with HttpClient() as http:
        gate = RobotsGate(http)
        if not await gate.allows(SITEMAP):
            log.error("robots.txt does not permit crawling %s; refusing", SITEMAP)
            return

        urls = [u for u in LOC_RE.findall(await http.get_text(SITEMAP))
                if MEASURE_URL_RE.search(u)]
        if limit:
            urls = urls[:limit]
        log.info("checking %d measure pages", len(urls))

        measures = []
        for url in urls:
            if not await gate.allows(url):
                continue
            try:
                parsed = parse_measure(url, await http.get_text(url))
            except Exception as exc:  # noqa: BLE001 — one bad page is not fatal
                log.warning("could not parse %s: %s", url, exc)
                continue
            if parsed:
                measures.append(parsed)
                log.info("  %-34s %s", parsed["measure"],
                         list(parsed["by_house_type"]))

    OUTPUT.write_text(json.dumps({
        "source": "Verbeterjehuis.nl (Milieu Centraal)",
        "source_url": "https://www.verbeterjehuis.nl/",
        "note": ("National indicative figures, not a quote. Costs and subsidies "
                 "vary by property, contractor and year."),
        "measures": measures,
    }, indent=2, ensure_ascii=False))
    log.info("wrote %d measures with cost tables to %s", len(measures), OUTPUT)


def cli() -> None:
    parser = argparse.ArgumentParser(description="Build the renovation cost table.")
    parser.add_argument("--limit", type=int, default=None, help="Only check the first N pages.")
    asyncio.run(main(parser.parse_args().limit))


if __name__ == "__main__":
    cli()
