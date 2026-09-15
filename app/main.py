"""Phase 2 entrypoint: the same services, exposed over REST.

Nothing in ``app/services`` imports FastAPI, so this file is pure wiring — the
proof that the Phase 1 engine is already mobile-app ready.
"""
from __future__ import annotations

import logging
import asyncio
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.gzip import GZipMiddleware
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.http_client import HttpClient
from app.core.inbound_rate_limit import InboundRateLimiter
from app.db.national_map_store import NationalMapStore
from app.core.logging import setup_logging
from app.db.repository import PropertyRepository
from app.notifiers.base import NullNotifier
from app.pipeline.orchestrator import Orchestrator
from app.pipeline.run_manager import RunManager
from app.services.region_filter import InvalidRegion, parse_regions
from app.models.property import ListingSource, PropertyListing
from app.scrapers.funda_url import FundaUrlUnparseable, parse_funda_url
from app.services.building import BuildingLookup, fill_listing
from app.services.geo.pdok_locatieserver import GeocodeError, PDOKLocatieserver
from app.models.market import MarketTrend
from app.services.commute import CommuteResult, CommuteService
from app.services.market import MarketLookup
from app.services.renovation import RenovationEstimate, RenovationEstimator
from app.services.woz import WozLookup, WozTrend
from app.models.enrichment import EnrichmentBundle
from app.models.score import HolisticScore, ScoredProperty
from app.services.enrichment import EnrichmentPipeline, GeocodeFailed
from app.services.scoring import ScoringEngine
from app.services.scoring import weights as W

log = logging.getLogger(__name__)

state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    http = HttpClient()
    await http.start()
    repo = PropertyRepository(settings.database_url)
    await repo.init_schema()
    state.update(http=http, repo=repo, enricher=EnrichmentPipeline(http),
                 scorer=ScoringEngine(), renovation=RenovationEstimator(),
                 runs=RunManager())
    yield
    # A cycle still in flight would keep using the client we are about to close.
    await state["runs"].cancel()
    await http.aclose()
    await repo.close()


app = FastAPI(title="WoonAgent API", version="0.1.0", lifespan=lifespan)
# The national map is ~14,700 features in one response; gzip is a free,
# zero-dependency win for that and costs nothing on the small responses
# everything else in this API returns.
app.add_middleware(GZipMiddleware, minimum_size=1024)

UI_FILE = Path(__file__).parent / "static" / "index.html"

#: Routes that answer before the gate. Only liveness: the platform health check
#: must not need the token, or a misconfigured secret looks like a dead app.
_UNGATED = frozenset({"/health"})
_ACCESS_COOKIE = "woonagent_access"

_rate_limiter = InboundRateLimiter(
    rate_per_minute=settings.rate_limit_per_minute, burst=settings.rate_limit_burst
)

#: A live-typing autocomplete fires far more often than a deliberate /score
#: click — every debounced keystroke, not every submit — but each call is one
#: lightweight PDOK lookup, not the roughly dozen-call fan-out /score costs.
#: Budgeting it against the same bucket would make typing an address feel
#: broken well before it ever approached the cost the main limiter exists to
#: bound, so autocomplete gets its own, much more generous tier.
_SUGGEST_PATHS = frozenset({"/geocode/suggest", "/geocode/address"})
_suggest_rate_limiter = InboundRateLimiter(rate_per_minute=120.0, burst=20)


def _real_client_ip(request) -> str:
    """The visitor's actual IP, not the one the socket sees.

    On Fly, every connection reaching this process comes from Fly's own edge
    proxy over an internal network (172.16-19.0.0/12 addresses) — that is
    ``request.client.host`` for literally every visitor, so it is useless for
    telling them apart or for rate-limiting them individually. Fly forwards
    the real address in ``Fly-Client-IP``; ``X-Forwarded-For`` is the more
    portable fallback (its first hop, since later ones can be appended by any
    proxy in between and are not to be trusted). Bare ``uvicorn --host`` with
    no edge in front of it, which is what a laptop is, has neither header, so
    the socket address is still the right final fallback there.
    """
    if fly_ip := request.headers.get("fly-client-ip"):
        return fly_ip
    if forwarded := request.headers.get("x-forwarded-for"):
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _client_key(request) -> str:
    """Identify the caller for rate-limiting, not for authorisation.

    The access token is the actual identity when one is configured — an IP
    behind Fly's edge or a home NAT is shared by more than one person, so
    keying on the token (when present) budgets the operator's account rather
    than everyone on their network. Falls back to the real client IP (see
    ``_real_client_ip``) so an unauthenticated flood of wrong-token guesses is
    still bounded — and so distinct anonymous visitors are not lumped into one
    shared bucket keyed on Fly's own internal proxy address.
    """
    token = request.headers.get("x-access-token") or request.query_params.get("key")
    return f"token:{token}" if token else f"ip:{_real_client_ip(request)}"


@app.middleware("http")
async def require_access_token(request, call_next):
    """Shared-secret gate plus a request budget, both active only when
    ``WOONAGENT_ACCESS_TOKEN`` is set.

    Unset — the local default — and this is a pass-through, so nothing changes
    on a laptop, including under the test suite's own rapid-fire requests.
    Set it before exposing the service publicly, for two separate reasons:

    * **The token** stops a stranger from using it at all. A single /score
      fans out to roughly a dozen requests against PDOK, CBS, the BAG and two
      volunteer-run routing servers, so an open endpoint is an amplifier aimed
      at infrastructure that is not yours.
    * **The rate limit** bounds what even a token holder can cost. On a host
      like Fly that scales to zero, a machine kept busy never goes idle and
      never stops billing — so a leaked token or a runaway script is a real
      dollar figure, not just bad manners, and the token alone does not cap it.

    Deliberately a shared secret and not user accounts. This is a personal tool
    with no per-user state; adding a login would be a lot of surface area to
    protect a threshold and a shortlist. If it ever needs real identities, that
    is the point to put a proper identity provider in front of it instead.
    """
    expected = settings.access_token
    if not expected or request.url.path in _UNGATED:
        return await call_next(request)

    limiter = _suggest_rate_limiter if request.url.path in _SUGGEST_PATHS else _rate_limiter
    allowed, retry_after = limiter.allow(_client_key(request))
    if not allowed:
        return JSONResponse(
            status_code=429, content={"detail": "too many requests"},
            headers={"Retry-After": str(int(retry_after) + 1)},
        )

    supplied = (request.headers.get("x-access-token")
                or request.query_params.get("key")
                or request.cookies.get(_ACCESS_COOKIE))

    # compare_digest, not ==, so a wrong guess cannot be narrowed down by
    # timing how long the rejection took.
    if not supplied or not secrets.compare_digest(supplied, expected):
        return JSONResponse(status_code=401, content={"detail": "access token required"})

    response = await call_next(request)
    # Remember it, so the UI's own fetches work after one ?key=... visit.
    if request.query_params.get("key"):
        response.set_cookie(_ACCESS_COOKIE, expected, httponly=True, samesite="lax",
                            secure=request.url.scheme == "https", max_age=60 * 60 * 24 * 90)
    return response


#: Separate from the app's own logger so `fly logs | grep visitors` or a
#: log-level filter can isolate this from application noise.
visitors = logging.getLogger("visitors")


@app.middleware("http")
async def log_visits(request, call_next):
    """One line per request: who, what, and how it went.

    This is the actual answer to "who is visiting this": ``fly logs --app
    woonagent`` (or the Live Logs view in the Fly dashboard) tails these.
    Deliberately just a log line and not a database table or a dashboard — a
    personal tool behind a shared token has no concept of a "user" to build a
    visitor table around, and Fly's own log retention (a few days on the free
    tier) is already what a home-grown table would give you for free, with
    none of the disk space or migration to maintain.

    Decorated AFTER require_access_token, and that ordering is load-bearing,
    not cosmetic: Starlette's ``add_middleware`` inserts each new middleware
    at the front of the stack, so the one decorated later ends up OUTERMOST
    and runs first on the way in. That is what makes a request rejected by the
    gate still show up here — the gate itself only ever tells you about
    traffic it accepted.
    """
    started = time.monotonic()
    response = await call_next(request)
    elapsed_ms = round((time.monotonic() - started) * 1000)
    visitors.info(
        "%s %s %s %dms ip=%s ua=%s",
        request.method, request.url.path, response.status_code, elapsed_ms,
        _real_client_ip(request), request.headers.get("user-agent", "-")[:80],
    )
    return response


@app.get("/")
async def index() -> dict[str, Any]:
    """A map of the API.

    Exists because the alternative is a bare 404 at the root of a service you
    have just started, which tells you nothing about whether it is working or
    where to go next.
    """
    return {
        "service": app.title,
        "version": app.version,
        "ui": "/ui",
        "docs": "/docs",
        "endpoints": {
            "GET /health": "liveness check",
            "GET /properties": "shortlist of everything scored so far",
            "GET /nearby": "properties already seen near a point, by distance",
            "GET /national-map": "every scored buurt in the country, as GeoJSON",
            "POST /run": "trigger one watch-loop cycle, optionally scoped to a region; GET /run for progress",
            "GET /geocode/suggest": "live address suggestions as you type",
            "GET /geocode/address": "postcode/number for a suggestion id",
            "POST /score": "score an ad-hoc address; body is a PropertyListing",
            "POST /score-url": "score a listing from its URL (parsed, not fetched)",
            "GET /weights": "the default per-dimension weighting",
            "POST /rescore": "re-weigh an existing bundle; no upstream calls",
            "POST /commute": "travel time from a property to places you name",
            "POST /renovation": "cost of moving between two energy labels",
        },
    }


@app.get("/ui", include_in_schema=False)
async def ui() -> FileResponse:
    """The browser front end. A single self-contained file — see app/static."""
    if not UI_FILE.exists():
        raise HTTPException(status_code=404, detail="UI not installed")
    return FileResponse(UI_FILE)


class CommuteRequest(BaseModel):
    """An origin address plus the places you actually need to get to."""

    postal_code: str
    house_number: str
    house_number_addition: Optional[str] = None
    destinations: list[dict] = Field(default_factory=list)


@app.post("/commute", response_model=CommuteResult)
async def commute(request: CommuteRequest) -> CommuteResult:
    """Travel time from one property to several destinations, by car and bike.

    Separate from /score so destinations can be edited without re-running the
    whole enrichment — the routing is the slow part and the score does not
    depend on it.
    """
    try:
        origin = await PDOKLocatieserver(state["http"]).resolve(
            postal_code=request.postal_code,
            house_number=request.house_number,
            addition=request.house_number_addition,
        )
    except GeocodeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return await CommuteService(state["http"]).travel_times(origin, request.destinations)


class RenovationRequest(BaseModel):
    """What would it cost to get this house from one label to another."""

    current_label: str = Field(..., description="Energy label today, e.g. 'F'.")
    target_label: str = Field(..., description="Energy label wanted, e.g. 'B'.")
    house_type: Optional[str] = Field(
        None, description="terraced | end_terrace | semi_detached | detached."
    )
    units_in_building: Optional[int] = None


@app.post("/renovation", response_model=RenovationEstimate)
async def renovation(request: RenovationRequest) -> RenovationEstimate:
    """Indicative cost of moving between two energy labels.

    User-triggered rather than computed with the score: the target is a choice
    only the buyer can make, and the answer changes entirely with it.
    """
    estimate = state["renovation"].between(
        current_label=request.current_label,
        target_label=request.target_label,
        house_type=request.house_type,
        units_in_building=request.units_in_building,
    )
    if estimate is None:
        raise HTTPException(
            status_code=503,
            detail=("No cost table available. Build it with "
                    "`uv run python scripts/build_renovation_costs.py`."),
        )
    return estimate


class RescoreRequest(BaseModel):
    """Re-weigh a score that has already been computed."""

    enrichment: EnrichmentBundle = Field(
        ..., description="The bundle returned by /score, handed straight back."
    )
    weights: dict[str, float] = Field(
        default_factory=dict,
        description=("Per-dimension weights, any subset. Unspecified dimensions keep "
                     "their default, and the set is renormalised to sum to 1.0."),
    )


@app.get("/weights")
async def default_weights() -> dict[str, float]:
    """The calibrated default weighting.

    Exists so a client can render the sliders without hardcoding the numbers;
    a copy in the front end would drift the first time these are retuned.
    """
    return {dimension.value: weight for dimension, weight in W.WEIGHTS.items()}


@app.post("/rescore", response_model=HolisticScore)
async def rescore(request: RescoreRequest) -> HolisticScore:
    """Re-score an existing bundle under a different weighting.

    Separate from /score for the reason that matters: scoring is a pure
    function of the enrichment bundle, so this endpoint performs no geocode and
    no provider calls. Re-weighing through /score instead would cost about a
    dozen requests to PDOK, CBS, the BAG and the routing servers *per slider
    drag*, which is precisely the load this codebase is careful not to
    generate.
    """
    try:
        weighting = W.normalise(request.weights)
    except W.InvalidWeights as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return ScoringEngine(weighting).score(request.enrichment)


class RunRequest(BaseModel):
    """Optional overrides for one triggered cycle.

    Sent from the browser rather than read from settings, because the daemon's
    ``WOONAGENT_REGIONS`` and a one-off browser trigger are different concerns:
    the daemon runs unattended on whatever region you configured once, and this
    lets the button try a different region without restarting it.
    """

    regions: Optional[str] = Field(
        None,
        description=("Comma-separated cities and/or postcode prefixes, e.g. "
                     "'Amsterdam,1018'. Omitted or blank falls back to "
                     "WOONAGENT_REGIONS, then to unfiltered."),
    )


@app.post("/run", status_code=202)
async def start_run(request: RunRequest = RunRequest()) -> dict[str, Any]:
    """Trigger one watch-loop cycle — the browser equivalent of `woonagent --once`.

    202, not 200: a cycle scrapes up to ``max_listings_per_cycle`` listings with
    a politeness pause between detail pages and about a dozen enrichment calls
    each, so it runs for minutes. It is accepted and run in the background; poll
    GET /run for progress.

    A second request while one is in flight is refused with 409 rather than
    queued. Two concurrent cycles would double the request rate every upstream
    sees, and the rate limiter is per-process and sized for one caller.
    """
    manager: RunManager = state["runs"]

    try:
        region_filter = parse_regions(
            request.regions if request.regions is not None else settings.regions
        )
    except InvalidRegion as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Telegram is optional for a browser-triggered cycle, and required for the
    # daemon. The CLI's job is to alert you while you are not looking, so it
    # should fail loudly when it cannot; this button's job is to fill the
    # shortlist you are looking at right now, and refusing to run because no
    # chat is configured would make it useless to anyone who only wants the UI.
    # The distinction is stated in the log rather than left implicit.
    configured = bool(settings.telegram_bot_token and settings.telegram_chat_id)
    notifier = None if configured else NullNotifier()
    if not configured:
        manager.note("no Telegram configured — scoring only, no alerts will be sent")

    def factory():
        orchestrator = Orchestrator(state["http"], state["repo"],
                                    notifier=notifier, progress=manager.note,
                                    region_filter=region_filter)
        return orchestrator.run_once()

    if not manager.start(factory, alerts_enabled=configured):
        raise HTTPException(status_code=409, detail="a cycle is already running")
    return manager.snapshot()


@app.get("/run")
async def run_status() -> dict[str, Any]:
    """Progress of the current or most recent cycle.

    Reports the last run's outcome after it finishes rather than reverting to
    idle, so a cycle that completed between two polls is still visible.
    """
    return state["runs"].snapshot()


@app.delete("/run", status_code=200)
async def cancel_run() -> dict[str, Any]:
    """Stop the in-flight cycle. Whatever it already stored is kept."""
    manager: RunManager = state["runs"]
    if not await manager.cancel():
        raise HTTPException(status_code=409, detail="no cycle is running")
    return manager.snapshot()


@app.get("/geocode/suggest")
async def geocode_suggest(
    q: str = Query(..., min_length=1, description="Partial address, as typed."),
) -> list[dict[str, str]]:
    """Live address suggestions, the way Google Maps' search box works.

    Backed by PDOK's own ``/suggest`` endpoint rather than exposing it to the
    browser directly: PDOK does not publish a CORS policy for it, and routing
    through this server keeps it behind the same access token and its own
    rate-limit tier as everything else, instead of every visitor's browser
    needing to be trusted to call a third party directly.
    """
    return await PDOKLocatieserver(state["http"]).suggest(q)


@app.get("/geocode/address")
async def geocode_address(id: str = Query(..., description="A suggestion id from /geocode/suggest.")) -> dict:
    """Postcode and house number for a suggestion the user picked.

    Exists only to save typing what was already selected from a dropdown.
    Scoring still runs the address through PDOKLocatieserver.resolve() from
    scratch when POST /score is called — this endpoint never feeds a
    GeoIdentity into a score, so there is exactly one code path that decides
    what an address resolves to.
    """
    try:
        return await PDOKLocatieserver(state["http"]).address_by_id(id)
    except GeocodeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/national-map")
async def national_map() -> dict[str, Any]:
    """Every scored buurt in the country, as one GeoJSON FeatureCollection.

    Built offline by ``scripts/build_national_map.py`` — this route only
    reads the finished file. DuckDB's Python driver is synchronous, so the
    read is pushed to a worker thread rather than blocking the event loop for
    however long it takes to pull ~14,700 rows.
    """
    db_path = Path(settings.national_map_db_path)
    if not db_path.exists():
        raise HTTPException(
            status_code=503,
            detail=("No national map data available. Build it with "
                    "`uv run python scripts/build_national_map.py`."),
        )

    def read() -> dict[str, Any]:
        store = NationalMapStore(db_path, read_only=True)
        try:
            return store.as_geojson()
        finally:
            store.close()

    return await asyncio.to_thread(read)


@app.get("/nearby")
async def nearby(
    lat: float = Query(..., ge=50.0, le=54.0, description="Latitude of the property."),
    lon: float = Query(..., ge=3.0, le=8.0, description="Longitude of the property."),
    radius_m: float = Query(2000.0, ge=100.0, le=10000.0),
    limit: int = Query(10, ge=1, le=50),
    exclude: Optional[str] = Query(None, description="property_id to leave out — usually this one."),
) -> list[dict[str, Any]]:
    """Other properties this agent has already seen near a point, nearest first.

    Asking prices from the local store, not sold prices: recent transactions
    are Kadaster's and licensed, and CBS publishes sale prices no finer than
    the municipality. Reads the database only, so it costs no upstream call.

    The bounds are the Netherlands. Everything behind this service is Dutch, so
    a point outside it is a mistake rather than an empty answer.
    """
    return await state["repo"].nearby(
        latitude=lat, longitude=lon, radius_m=radius_m, limit=limit,
        exclude_property_id=exclude,
    )


@app.get("/properties")
async def properties(
    limit: int = Query(50, ge=1, le=200),
    min_score: float = Query(0.0, ge=0.0, le=10.0),
) -> list[dict[str, Any]]:
    """Everything the watch loop has scored, best first.

    Reads the store rather than the network, so it is instant and works with
    the agent stopped.
    """
    return await state["repo"].top_scored(limit=limit, min_score=min_score)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


class UrlScoreRequest(BaseModel):
    """Score a listing given only its URL, plus whatever the page showed."""

    url: str
    construction_year: Optional[int] = None
    price_eur: Optional[int] = None
    living_area_m2: Optional[int] = None


@app.post("/score-url", response_model=ScoredProperty)
async def score_url(request: UrlScoreRequest) -> ScoredProperty:
    """Resolve a listing URL to an address, then score it.

    The URL is parsed, never fetched — see app/scrapers/funda_url.py. Price,
    floor area and construction year live only in the page body, so they are
    accepted from the caller instead.
    """
    try:
        parsed = parse_funda_url(request.url)
    except FundaUrlUnparseable as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        geo = await PDOKLocatieserver(state["http"]).resolve_text(
            street=parsed.street, house_number=parsed.house_number,
            city=parsed.city, addition=parsed.addition,
        )
    except GeocodeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    postcode = _postcode_of(geo)
    if not postcode:
        raise HTTPException(status_code=422, detail="resolved address carried no postcode")

    listing = PropertyListing(
        property_id=f"url-{parsed.listing_id or postcode}",
        source=ListingSource.MANUAL,
        url=request.url,
        address=geo.matched_address or parsed.query,
        postal_code=postcode,
        house_number=parsed.house_number,
        house_number_addition=parsed.addition,
        construction_year=request.construction_year,
        price_eur=request.price_eur,
        living_area_m2=request.living_area_m2,
    )

    # The URL gives an address and nothing more, so the registries fill in what
    # the listing page would have shown. Anything the caller supplied wins.
    facts = await BuildingLookup(state["http"]).for_location(
        geo, postal_code=postcode, house_number=parsed.house_number,
        addition=parsed.addition)
    listing = fill_listing(listing, facts)

    bundle = await state["enricher"].enrich_one(listing, geo=geo)
    return ScoredProperty(listing=listing, enrichment=bundle,
                          score=state["scorer"].score(bundle),
                          woz=await _woz_for(geo),
                          market=await _market_for(geo, listing),
                          building=facts)


def _postcode_of(geo) -> Optional[str]:
    match = re.search(r"\b(\d{4}\s?[A-Z]{2})\b", geo.matched_address or "")
    return match.group(1).replace(" ", "") if match else None


@app.post("/score", response_model=ScoredProperty)
async def score_listing(listing: PropertyListing) -> ScoredProperty:
    """Score an ad-hoc address without persisting it — the mobile app's
    "check this house" button."""
    # Resolve first, so the registries can fill blanks before scoring — the
    # same treatment /score-url gives. Without this the two endpoints would
    # disagree about the same house depending on how you asked.
    try:
        geo = await PDOKLocatieserver(state["http"]).resolve(
            postal_code=listing.postal_code,
            house_number=listing.house_number,
            addition=listing.house_number_addition,
        )
    except GeocodeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    facts = await BuildingLookup(state["http"]).for_location(
        geo, postal_code=listing.postal_code, house_number=listing.house_number,
        addition=listing.house_number_addition)
    listing = fill_listing(listing, facts)

    try:
        bundle = await state["enricher"].enrich_one(listing, geo=geo)
    except GeocodeFailed as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ScoredProperty(
        listing=listing, enrichment=bundle, score=state["scorer"].score(bundle),
        woz=await _woz_for(geo),
        market=await _market_for(geo, listing),
        building=facts,
    )


async def _market_for(geo, listing) -> Optional[MarketTrend]:
    """Best-effort: a missing market trend must not fail a score."""
    if not geo or not geo.gemeentecode:
        return None
    try:
        return await MarketLookup(state["http"]).trend(
            geo.gemeentecode, asking_price_eur=listing.price_eur,
            gemeente_name=geo.gemeentenaam)
    except Exception as exc:  # noqa: BLE001
        log.warning("market trend lookup failed: %s", exc)
        return None


async def _woz_for(geo) -> Optional[WozTrend]:
    """Best-effort: a missing valuation trend must not fail a score."""
    if not geo or not geo.buurtcode:
        return None
    try:
        return await WozLookup(state["http"]).trend(geo.buurtcode)
    except Exception as exc:  # noqa: BLE001
        log.warning("WOZ trend lookup failed: %s", exc)
        return None
