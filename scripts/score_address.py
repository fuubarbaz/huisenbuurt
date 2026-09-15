#!/usr/bin/env python3
"""Score one address from the command line.

    woonagent-score 1181AA 1 --year 1975 --price 650000 --area 120
    woonagent-score https://www.funda.nl/detail/koop/amsterdam/huis-singel-30/43829102/ --year 1890

The first argument is either a PC6 postcode or a listing URL. A Funda URL is
parsed for its address and geocoded — Funda itself is never fetched, so price,
floor area and construction year have to come from the flags.

Runs the full enrichment pipeline and the scoring engine against the live open
data APIs. This is the working end of the engine: everything from a postcode to
a scored report. It does not touch the database or send anything.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.http_client import HttpClient  # noqa: E402
from app.core.logging import setup_logging  # noqa: E402
from app.models.property import ListingSource, PropertyListing  # noqa: E402
from app.models.score import ScoredProperty  # noqa: E402
from app.services.enrichment import EnrichmentPipeline, GeocodeFailed  # noqa: E402
from app.scrapers.funda_url import FundaUrlUnparseable, parse_funda_url  # noqa: E402
from app.services.building import BuildingLookup, fill_listing  # noqa: E402
from app.services.geo.pdok_locatieserver import (  # noqa: E402
    GeocodeError,
    PDOKLocatieserver,
)
from app.services.scoring import ScoringEngine  # noqa: E402


def looks_like_url(value: str) -> bool:
    return value.startswith(("http://", "https://")) or "funda.nl/" in value


def _postcode_from(geo) -> str | None:
    """Recover the PC6 from the matched address string."""
    if not geo or not geo.matched_address:
        return None
    match = re.search(r"\b(\d{4}\s?[A-Z]{2})\b", geo.matched_address)
    return match.group(1).replace(" ", "") if match else None

BAR_WIDTH = 10
SEVERITY_ICON = {"critical": "🔴", "warn": "🟠", "info": "🔵"}


def bar(score: float) -> str:
    filled = int(round(score * BAR_WIDTH / 10))
    return "█" * filled + "░" * (BAR_WIDTH - filled)


def render(item: ScoredProperty, duration_ms: int | None, building=None) -> str:
    listing, bundle, score = item.listing, item.enrichment, item.score
    geo = bundle.geo if bundle else None
    out: list[str] = []

    out.append("")
    out.append(f"  {geo.matched_address if geo else listing.address}")
    if geo:
        out.append(f"  {geo.buurtnaam or '?'} · {geo.gemeentenaam or '?'} · {geo.provincie or '?'}")

    facts = []
    if listing.price_eur:
        facts.append(f"€{listing.price_eur:,}".replace(",", "."))
    if listing.living_area_m2:
        facts.append(f"{listing.living_area_m2} m²")
    if listing.price_per_m2:
        facts.append(f"€{listing.price_per_m2:,.0f}/m²".replace(",", "."))
    if listing.construction_year:
        facts.append(f"built {listing.construction_year}")
    if facts:
        out.append("  " + " · ".join(facts))
    if building:
        extra = []
        if building.energy_label:
            extra.append(f"energy label {building.energy_label}")
        if building.units_in_building and building.units_in_building > 1:
            extra.append(f"{building.units_in_building} units in the building")
        if building.status and building.status != "Pand in gebruik":
            extra.append(building.status.lower())
        if extra:
            out.append("  " + " · ".join(extra) + "   [BAG]")

    if score:
        out.append("")
        out.append(f"  {bar(score.total_score)}  {score.total_score}/10")
        out.append(f"  data coverage {score.data_coverage_pct}%  ·  confidence {score.confidence}")
        out.append("")
        for d in score.dimensions:
            flag = "~" if d.imputed else " "
            out.append(f" {flag}{d.dimension.value:26} {bar(d.score)} {d.score:5.2f}  ×{d.weight:.2f}")
            for driver in d.drivers:
                out.append(f"      · {driver}")

        if score.risk_flags:
            out.append("")
            out.append("  Flags")
            for f in score.risk_flags:
                out.append(f"   {SEVERITY_ICON.get(f.severity, '•')} {f.message}")

    if bundle:
        failed = bundle.failures()
        if failed:
            out.append("")
            out.append("  Degraded layers")
            for name, reason in failed.items():
                out.append(f"   · {name}: {reason}")
        out.append("")
        out.append(f"  enriched in {bundle.total_duration_ms} ms"
                   + (f" (total {duration_ms} ms)" if duration_ms else ""))
    out.append("")
    return "\n".join(out)


async def main(args: argparse.Namespace) -> int:
    if args.verbose:
        setup_logging()

    building = None
    postal_code, house_number, addition = args.postcode, args.house_number, args.addition
    address = f"{postal_code} {house_number}" if postal_code else args.target
    source_url = args.url or "https://example.invalid/cli"
    geo = None

    if looks_like_url(args.target):
        try:
            parsed = parse_funda_url(args.target)
        except FundaUrlUnparseable as exc:
            print(f"could not read an address from that URL: {exc}", file=sys.stderr)
            return 2
        # The URL carries no postcode, so resolve it here and hand the result
        # to the pipeline — which then skips its own geocode step.
        async with HttpClient() as http:
            try:
                geo = await PDOKLocatieserver(http).resolve_text(
                    street=parsed.street, house_number=parsed.house_number,
                    city=parsed.city, addition=parsed.addition,
                )
            except GeocodeError as exc:
                print(f"could not resolve {parsed.query!r}: {exc}", file=sys.stderr)
                return 2
        postal_code, house_number, addition = (
            geo.matched_address.split(",")[-1].strip().split()[0]
            if geo.matched_address else "",
            parsed.house_number,
            parsed.addition,
        )
        postal_code = _postcode_from(geo) or postal_code
        address = geo.matched_address or parsed.query
        source_url = args.url or args.target

    listing = PropertyListing(
        property_id=f"cli-{postal_code}-{house_number}",
        source=ListingSource.MANUAL,
        url=source_url,
        address=address,
        postal_code=postal_code,
        house_number=house_number,
        house_number_addition=addition,
        construction_year=args.year,
        price_eur=args.price,
        living_area_m2=args.area,
    )

    async with HttpClient() as http:
        if not args.no_registry:
            # Fill blanks from the national registries before scoring: the
            # construction year drives the foundation check, and BAG is a
            # better source for it than a half-remembered figure.
            if geo is None:
                try:
                    geo = await PDOKLocatieserver(http).resolve(
                        postal_code=postal_code, house_number=house_number, addition=addition)
                except GeocodeError as exc:
                    print(f"could not geocode that address: {exc}", file=sys.stderr)
                    return 2
            facts = await BuildingLookup(http).for_location(
                geo, postal_code=postal_code, house_number=house_number,
                addition=addition)
            filled = fill_listing(listing, facts)
            if filled is not listing:
                gained = [f for f in ("construction_year", "living_area_m2")
                          if getattr(filled, f) != getattr(listing, f)]
                print(f"  (from BAG: {', '.join(gained)})", file=sys.stderr)
            listing, building = filled, facts

        pipeline = EnrichmentPipeline(http)
        try:
            bundle = await pipeline.enrich_one(listing, geo=geo, only=args.only)
        except GeocodeFailed as exc:
            print(f"could not geocode that address: {exc}", file=sys.stderr)
            return 2

    scored = ScoredProperty(
        listing=listing, enrichment=bundle, score=ScoringEngine().score(bundle)
    )

    if args.json:
        print(json.dumps(scored.model_dump(mode="json"), indent=2, ensure_ascii=False))
    else:
        print(render(scored, bundle.total_duration_ms, building))
    return 0


def cli() -> None:
    """Console entry point: ``woonagent-score``."""
    p = argparse.ArgumentParser(description="Score one Dutch address.")
    p.add_argument("target", metavar="POSTCODE|URL",
                   help="A PC6 such as 1015AA, or a Funda listing URL.")
    p.add_argument("house_number", nargs="?",
                   help="House number. Required with a postcode, ignored with a URL.")
    p.add_argument("--addition", help="House number addition, e.g. 'A' or 'bis'.")
    p.add_argument("--year", type=int, help="Construction year. Drives the foundation-risk check.")
    p.add_argument("--price", type=int, help="Asking price in euros.")
    p.add_argument("--area", type=int, help="Living area in m².")
    p.add_argument("--url", help="Listing URL, for the report header.")
    p.add_argument("--no-registry", action="store_true",
                   help="Skip the BAG/EP-Online lookup that fills in missing details.")
    p.add_argument("--only", nargs="*", help="Restrict to these layers, e.g. --only noise soil")
    p.add_argument("--json", action="store_true", help="Emit the full record as JSON.")
    p.add_argument("-v", "--verbose", action="store_true", help="Show request logging.")
    args = p.parse_args()
    if looks_like_url(args.target):
        args.postcode = None
    else:
        if not args.house_number:
            p.error("a house number is required when the first argument is a postcode")
        args.postcode = args.target
    raise SystemExit(asyncio.run(main(args)))


if __name__ == "__main__":
    cli()
