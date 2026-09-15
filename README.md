# WoonAgent

Real-estate intelligence engine for the Dutch buying market. Monitors new
listings, enriches each address with Dutch public open data, computes a
"Holistic Neighbourhood & Property Score", and pushes an alert to Telegram.

## Phase 1 (this repo) → Phase 2 (mobile)

Nothing under `app/services/` or `app/models/` imports FastAPI, SQLite, or
Telegram. The services take arguments and return Pydantic models. `app/main.py`
is therefore pure wiring — a REST facade over the same engine the CLI runs.

```
run_agent.py  →  Orchestrator  →  services  ←  main.py (FastAPI, Phase 2)
```

## Layout

```
woonagent/
├── run_agent.py                   # Phase 1 entrypoint (--once for a single cycle)
├── pyproject.toml                 # deps + console scripts; uv.lock pins them
├── .env.example                   # all keys prefixed WOONAGENT_
│
└── app/
    ├── main.py                    # Phase 2 FastAPI facade
    │
    ├── core/
    │   ├── config.py              # Settings + every upstream endpoint URL
    │   ├── http_client.py         # backoff + jitter, UA rotation, pooled client
    │   ├── rate_limit.py          # per-host token buckets, 5-15 min poll jitter
    │   └── logging.py
    │
    ├── models/                    # the API contract, shared by every layer
    │   ├── property.py            # PropertyListing, GeoIdentity
    │   ├── enrichment.py          # per-layer payloads + ProviderResult envelope
    │   └── score.py               # DimensionScore, RiskFlag, ScoredProperty
    │
    ├── db/
    │   ├── schema.sql             # property_id PK; notifications table = alert guard
    │   └── repository.py          # the only module that speaks SQL
    │
    ├── scrapers/                  # base.py + funda.py + huispedia.py
    │
    ├── services/
    │   ├── geo/pdok_locatieserver.py     # address → lat/lon + CBS buurtcode
    │   ├── enrichment/
    │   │   ├── enrichment_pipeline.py    # fan-out orchestration
    │   │   └── providers/                # one module per open-data source
    │   │       ├── base.py               # timing, timeout, error containment
    │   │       ├── leefbaarometer.py     # livability grid (WMS GetFeatureInfo)
    │   │       ├── politie.py            # registered crime, per 1000 inhabitants
    │   │       ├── cbs_demographics.py   # households with children
    │   │       ├── duo_education.py      # schools + inspection ratings
    │   │       ├── rivm_noise.py         # Lden / Lnight per source
    │   │       └── soil_risk.py          # funderingsrisico, paalrot, flood
    │   └── scoring/
    │       ├── weights.py                # the whole tuning surface
    │       └── scoring_engine.py         # pure, synchronous, no I/O
    │
    ├── notifiers/
    │   ├── base.py                       # Notifier ABC + NullNotifier
    │   ├── telegram.py                   # delivery only
    │   └── formatters/markdown_card.py   # rendering only
    │
    └── pipeline/orchestrator.py   # the only module that knows the run order
```

## Score composition

| Weight | Dimension | Sources |
|--------|-----------|---------|
| 25% | Physical safety | Politie / CBS registered crime |
| 20% | Peer & family concentration | CBS Kerncijfers wijken en buurten |
| 20% | Education quality & access | DUO + Onderwijsinspectie |
| 20% | Environmental health | RIVM noise + Leefbaarometer |
| 15% | Structural security | Klimaateffectatlas + Bodemloket |

Weights live in `app/services/scoring/weights.py` and are asserted to sum to 1.0.

## Design rules

- **Degrade, never abort.** Every provider is wrapped so a timeout or a 5xx
  becomes a `ProviderResult(status=ERROR)`. A bundle at 60% coverage is still
  scored; `confidence` and `data_coverage_pct` carry the caveat.
- **`property_id` is the primary key.** `upsert_listing()` returns `True` only
  for genuinely new rows — that is the sole duplicate-processing guard. The
  `notifications` table is the separate duplicate-*alert* guard.
- **Fetching never notifies.** The enrichment pipeline performs no writes and
  sends no messages; it returns a model.

## Running it

Managed with [uv](https://docs.astral.sh/uv/). One command creates the
environment, installs everything and pins it:

```bash
uv sync
```

`uv.lock` is committed and is the real version pin — `pyproject.toml` carries
only lower bounds, so there is one place to update rather than two.

**Score an address** — this is the working end of the engine today. No database,
no Telegram, no config needed:

```bash
uv run woonagent-score 1181AA 1 --year 1975 --price 650000 --area 120
```

`--year` matters: it drives the foundation-risk check. Add `--json` for the full
record, `--only noise soil` to run selected layers, `-v` for request logging.

**Build the school index** once, or the education layer reports `MISSING`:

```bash
uv run woonagent-build-index
```

**Run the REST API** (Phase 2 facade — the same services, over HTTP):

```bash
uv run uvicorn app.main:app --reload
```

Then open **<http://localhost:8000/ui>**.

| Route | |
|---|---|
| `GET /ui` | browser front end (React, no build step) |
| `GET /` | index of the API |
| `GET /properties` | everything the watch loop has scored, best first |
| `GET /docs` | interactive OpenAPI docs |
| `GET /health` | liveness check |
| `POST /score` | score an ad-hoc address; body is a `PropertyListing` |
| `POST /score-url` | score a listing from its URL (parsed, never fetched) |
| `POST /commute` | travel time to places you name, by car and bike |
| `POST /renovation` | cost of moving between two energy labels |

`/score` is POST-only, so opening it in a browser returns 405 — use `/docs` to
try it interactively.

**Run the tests** (332, no network needed):

```bash
uv run pytest -q
```

**Run the watch loop** — one cycle, or continuously on a randomised 5-15 minute
interval:

```bash
WOONAGENT_DRY_RUN=true uv run woonagent --once
```

`WOONAGENT_DRY_RUN=true` scores and stores without sending anything, which is
the right way to try it. For real alerts set `WOONAGENT_TELEGRAM_BOT_TOKEN` and
`WOONAGENT_TELEGRAM_CHAT_ID`, drop the dry-run flag, and omit `--once`.
`WOONAGENT_MAX_LISTINGS_PER_CYCLE` (default 25) bounds the work per cycle, and
`WOONAGENT_MIN_SCORE_TO_NOTIFY` (default 6.5) sets the alert bar.

## Status

**All six enrichment layers and all five scoring dimensions are live** against
the real APIs: PDOK geocoder, CBS demographics, Politie crime, RIVM noise,
Leefbaarometer, RVO foundation risk, DUO schools. A full enrichment runs in
about one second. 332 tests. Only the REST tests need FastAPI; none need the network.

Ingestion and persistence are live too: the Huispedia scraper discovers new
listings from the site's sitemaps, and the repository makes the loop safe to
leave running. Verified against live data — a first cycle alerts on five new
listings, a second cycle over the same store sends nothing.

**The pipeline is feature-complete.** No `TODO` markers remain in `app/`.

### Commute

Add the places you actually need to reach — work, family, a station — and get
door-to-door times by car and bike. Destinations live in browser storage, so
they carry over from one property to the next; `POST /commute` is separate from
`/score` so editing them does not re-run the enrichment.

**Two routers, deliberately.** OSRM's public demo server carries only the
driving graph and answers *every* profile from it: `cycling`, `walking` and
`foot` between the same two points return byte-identical duration and distance
to `driving` (8.8 min / 11.06 km for all five, verified). So a bike time taken
from OSRM would be a car time with a different label. Cars go to OSRM, bikes to
**BRouter**, which routes that same journey as 5.2 km in 15 min — shorter and
more direct, which is what a bike does in a Dutch city.

**Public transport needs a key.** No keyless door-to-door transit router exists
for the Netherlands: NS, transit.land and OpenRouteService all return 401 and
the public OTP instances are gone. Set `WOONAGENT_ORS_API_KEY` to enable the
column; without it, it is absent rather than guessed at.

**Paste from Google Maps.** A destination accepts an address copied out of
Maps (the trailing "Netherlands" no longer blocks the match), a `maps/place/…`
URL, or a bare coordinate pair from *copy coordinates*. Where a URL carries a
point it is used directly, which is more precise than any text search; where it
carries only a name, the name is extracted and searched. A coordinate outside
the Netherlands is refused rather than scored, since every source behind this
engine is Dutch. Short `maps.app.goo.gl` links are not resolved — open one and
copy the full URL.

**Destinations are verified, not trusted.** Locatieserver always answers, and
loosely: *"Utrecht Centraal"* resolves to a bus station in **Breda**, and
*"Schiphol Airport"* to **Maastricht**. Each shares one generic word with the
query and nothing else. A confidently wrong commute time is worse than none, so
a match that drops a distinctive word is rejected with a prompt to use a
postcode. Generic words — station, centraal, airport — never carry a match on
their own.

Both routers are shared community services run on donated capacity; they are
called once per destination and rate-limited to 1 request/second.

### This home, versus its neighbourhood

Panels are labelled by scope, because blurring the two is how a buyer talks
themselves into the wrong house. A **This home** panel leads with what is
measured at the address itself — construction year and floor area for this
dwelling (BAG, by its own BAG id), asking price and price per m², energy label
(EP-Online, per dwelling), foundation risk (PC6), the noise reading sampled at
this coordinate, and the 100 m Leefbaarometer cell. Every row names its source.

Everything below it carries an `area`, `area average` or `municipality` chip:
demographics and crime are buurt figures, WOZ is the average of every dwelling
in the buurt, market pressure is the municipality.

**Per-address WOZ is not available.** The WOZ-waardeloket publishes no
documented API — unlike EP-Online, which turned out to have a public OpenAPI
spec — and its lookup is a single-page app whose endpoints are undocumented.
The WOZ panel is therefore the area average and says so. What the engine *can*
compare per-address is the asking price per m² against that average.

### Who lives here

A **Who lives here** panel shows the demographic picture the family dimension
scores one number out of: the full age profile as a stacked bar (0-15, 15-25,
25-45, 45-65, 65+), household composition (with children, without, single
person), tenure (owner-occupied, rented, social) and the house/flat split.

All of it comes free in the KWB row the demographics provider already fetches —
no extra request. It is worth showing because a single number misleads:
Amstelveen and Amsterdam's Singel both have ~32% of residents aged 25-45, but
one is 36% families and 44% houses, the other 59% single-person and 91% flats.

Income is deliberately absent. `GemiddeldInkomenPerInwoner` and the two income
quantile columns exist in the 2025 edition but are null for every buurt checked
— the same empty-column trap as the gas-use field. A field that is always
`None` is worse than no field.

### Recorded crime in the UI

The scoring dimension answers "is this area safer than average"; the crime
panel answers the question a buyer actually asks — *what happened here?* It
shows absolute offences over the trailing twelve months (burglary, vandalism,
nuisance, violence), each with its ratio to the national rate for that same
offence, plus a monthly bar chart of all recorded crime so a rising or falling
trend is visible rather than inferred.

Counts come first and ratios second, deliberately: "9 burglaries" is the fact,
"1.34× national" is the context for it. An offence group with no rows is
omitted rather than shown as a confident zero.

Read these with the denominator caveat above in mind — crime is recorded where
it happens, not where the victims live.

### Energy renovation estimate

Verbeterjehuis.nl is run by Milieu Centraal, a public-interest foundation, and
its robots.txt permits crawling outright. Each insulation measure page carries
a cost table by house type — investment, subsidy, gas saved, euros saved.

```bash
uv run python scripts/build_renovation_costs.py
```

That runs **once** into `data/renovation_costs.json`; scoring a property then
never touches their site. Four measures publish that table: facade, floor and
roof insulation, and glazing. Heat pumps and solar present costs differently on
their site and are not extracted.

### Getting the energy label

Set `WOONAGENT_EPONLINE_API_KEY` — the key is free from RVO at
<https://www.ep-online.nl>, and the label then takes precedence over the
build-year heuristic below.

The register is queried **by BAG id** where possible. EP-Online's OpenAPI spec
is public at `public.ep-online.nl/swagger/v5/swagger.json` (no key needed to
read it) and documents two endpoints:

| Endpoint | Used for |
|---|---|
| `GET /PandEnergielabel/AdresseerbaarObject/{id}` | preferred — the id is PDOK's `adresseerbaarobject_id`, naming one dwelling exactly |
| `GET /PandEnergielabel/Adres` | fallback, by postcode + number |

That matters in a block of flats, where a postcode and number match several
dwellings. The fallback also splits `huisletter` from `huisnummertoevoeging`:
the register treats `30-H` (a letter) and `30-bis` (a toevoeging) as different
kinds of thing, and sending one as the other returns no registration.

The response carries more than a letter. **`Gebouwtype` is the woningtype the
renovation cost tables are keyed on**, so a registered label also collapses the
renovation range from "terraced to detached" to a single figure.

Verified end to end on a real registration: Kwadijkerpark 45, 1444JE Purmerend
holds label **A+++**, and EP-Online indexes it under BAG verblijfsobject
`0439010000206342` — the exact id PDOK returns for that address. Only the
authenticated call itself is unverified, for want of a key. A test pins the
parsing to that record verbatim rather than to what the schema suggested.

A "no registration found" result is ordinary and not an error: a label exists
only once one has been commissioned. Number 69 in the same street returns
nothing because BAG has it as `Bouw gestart` — still under construction.

The two web lookups were investigated and are not used:
`energielabel.nl`'s API (`POST /api/energielabel/adressen/`) only resolves an
address to BAG ids that PDOK already provides — no label in it. And
`ep-online.nl/Energylabel/Search` is a server-rendered form behind an ASP.NET
`__RequestVerificationToken`; it does accept a BAG id, but driving it means
carrying a CSRF token and scraping HTML for data the documented JSON API
returns under a stable contract.

**You choose the target.** The panel asks for the label the house has now and
the one you want, then calls `POST /renovation`. The work between two labels is
a set difference — what a G-rated house still needs, minus what a B-rated one
still needs — so `G → C` prices three measures where `G → A` prices four, and
`C → A` prices one. Asking for a label you already hold, or a worse one, costs
nothing rather than erroring.

The current label is prefilled from EP-Online when the register has one and
stays editable: the register can be stale, and you may know the house better.
The estimate is deliberately *not* bundled into `/score` — the target is a
choice only the buyer can make, and the answer changes entirely with it.

There is still a `estimate()` path that infers outstanding work from the
construction year against the Dutch regulatory timeline (no insulation
requirement before 1976; first requirements 1976-91; standard from 1992), used
where no label is known at all. Era heuristics, not a survey.

The figures stay attributed and every measure links back to its page. It is an
indication, not a quote: the range spans terraced to detached because BAG does
not record which a house is, and real cost turns on the state of the fabric and
what has already been done, which no registry holds.

**For a flat the panel says so.** The roof, facade and floor belong to the VvE,
so the numbers are building-level context rather than the buyer's bill.

*homeggo.nl was requested too — its domain does not resolve.*

### Market pressure — and why it is not "overbidding"

The true overbidding percentage — what buyers paid above the asking price — is
**not open data**. It needs both numbers for the same transaction and only one
is public:

| Source | Has | Problem |
|---|---|---|
| CBS `83625NED` | average sale price, 728 municipalities, 1995-2025 | no asking price |
| CBS `82534NED` | asking prices | discontinued after **2016Q4**, and price *bands* only |
| NVM | overbidding, quarterly | PDF market reports, no API |
| Kadaster | every transaction | licensed commercially |

Searching the CBS catalogue for `overbieden` returns nothing at all.

What is computed instead is **average sale price ÷ average WOZ assessment**, a
proxy for market heat, since overbidding is what happens when buyers pay well
above assessed value. It tracks the real cycle: Amstelveen peaks at **1.42× in
2021**, falls to 1.17× in 2023 as the market corrected, and sits at 1.20× now.
Rotterdam and Amsterdam show the same shape.

Read the *movement*, not the level. The WOZ waardepeildatum is 1 January of the
preceding year, so a 2025 sale is measured against an eighteen-month-old
valuation; that lag alone puts the ratio above 1.0 in any rising market. The
panel says so, and a test asserts no field in the model is named "overbid".

Alongside it is a direct comparison that needs no caveat: **this asking price
against the municipality's latest average sale price**.

### WOZ valuation trend

CBS publishes the average WOZ per area in every annual Kerncijfers wijken en
buurten edition, so querying one area code across editions gives a real
multi-year trend from documented open data — Amstelveen's BU03620102 runs
€407k (2019) to €602k (2025), **+47.9%**.

It is deliberately **not scored**. How a neighbourhood is priced says little
about whether it suits a family, which is what the five dimensions measure.

*Per-address* WOZ history is not read. The WOZ-waardeloket shows a value for
one house but has no documented public API — only the endpoints its own SPA
calls — and guessing at those is the undocumented scraping this codebase
declines elsewhere.

**Boundaries move, and the series says so.** CBS renumbers buurt and wijk codes
when it redraws them: Amsterdam's `BU0363AC01` does not exist in the 2021
edition, nor does its wijk — only the gemeente survives. The trend is therefore
built from *one* area code across editions, and a year where that code is
absent is reported as a gap. Falling back to the gemeente for the missing years
would draw a line through two different geographies and call it a trend.

### The browser UI

`app/static/index.html` is a single self-contained React file served at `/ui`.
React and Babel come from a CDN and JSX compiles in the browser — slower than a
bundled build and the wrong choice for anything public, but for a tool served
by your own process it buys no `node_modules`, no build step and no second dev
server. Two tabs: score any address on demand, and browse what the watch loop
has already found. If it grows past a few screens, move it to Vite; the
component boundaries are already the ones you would keep.

### The alert

Each alert is a MarkdownV2 card: address and neighbourhood, the composite score
with a per-dimension breakdown, one line of supporting figures per data layer,
and the risk flags worst-first (capped at four, with a remainder note). Around
900 characters against Telegram's 4096 limit.

Every section is guarded on its provider result being usable, so a degraded run
produces a *shorter* card rather than one full of blanks — there is a test
asserting the string "None" never reaches a card. MarkdownV2 escaping is the
other sharp edge: one unescaped reserved character and Telegram rejects the
whole message with a 400, at delivery time. A test renders a maximal card and
asserts no reserved character survives unescaped outside deliberate markup.

Two presentation details worth knowing. School distances below 100 m render as
`<100 m`, because positions come from postcode geocoding and "0 m" would claim
a precision the data does not have. Foundation risk is labelled from the
language-neutral ordinal rather than the provider's Dutch class name, so the
card stays in one language.

### Three guards, deliberately separate

| Guard | Mechanism | Without it |
|---|---|---|
| Duplicate **processing** | `upsert_listing()` returns `True` only for a genuinely new row | Every cycle re-spends a geocode and six API calls per listing |
| Duplicate **alerting** | the `notifications` table, where only `success = 1` counts | You get pinged about the same house on every poll |
| Undelivered **retry** | `pending_notification()` sweep at the end of each cycle | A transient Telegram outage loses that alert permanently |

The third exists because the first two collide: a property is already known by
the next cycle, so the processing guard skips it long before the notify step is
reached. The sweep re-reads the stored score and re-sends — no re-enrichment.

### The education layer needs a one-off build

DUO publishes the school registry as bulk CSV with no coordinates and no
spatial query. Rather than download and geocode 6,096 schools per property,
the registry is geocoded once into `data/schools.db`:

```bash
uv run woonagent-build-index
```

Measured at 20.5 geocodes/sec, so about **4–5 minutes** cold for 5,535 distinct
postcodes. Re-runs after a registry refresh are nearly free: postcode
coordinates are cached permanently in the same file, and a build resumes from
whatever is already cached if it is interrupted. The index is a cache, not
state — deleting it costs a rebuild and nothing else. Until it exists the
education layer reports `MISSING` with the command to run, rather than quietly
scoring zero.

Positions come from geocoding each school's postcode, so they are good to
roughly 50–100 m. Read distances as "about 600 m", not as surveyed values.

### Inspection ratings are a 2018 snapshot

This is the weakest data in the engine and it is treated accordingly. DUO
publishes exactly one verdict file, `Peildatum` 2018-09-01, in which **81% of
primary schools are simply "Voldoende"** — 73 are "Goed" and 114 fall below par
out of 7,142. Eight years old, and barely ranks anything.

So the education dimension is built on **proximity and choice**, which are
current and do discriminate. Ratings enter only as a capped penalty (never more
than 1.0 point however many poor schools are nearby) and an info flag that
names the snapshot date and tells the reader to check the current report.
`Geen oordeel` and `Zonder actueel oordeel` are treated as *unrated*, which is
not the same as *poor* — there is a test pinning that distinction.

### Foundation risk is the highest-stakes layer

Pile rot (*paalrot*) needs **both** halves: ground where the water table moves,
and a foundation old enough to be timber. Vulnerable ground under a 2005 house
is a non-issue; a 1920 house on sand was never at risk. So the provider combines
RVO's area classification with **this property's own construction year**, not
the area's pre-1970 share — and says which it used, dropping confidence to 0.6
when the listing carried no year.

Urban postcodes come back as `Stedelijk gebied` with soil `Niet indeelbaar`,
because city ground is too disturbed to classify. The dataset's own guidance
fills the gap — *"cities in West and North Netherlands have vulnerable soil
areas"* — so province stands in for the missing soil class there. That is why
an 1890 house scores `hoog` in Amsterdam and `laag` in Maastricht.

*Known false positive:* the Wadden islands are sandy but classified
`Stedelijk gebied` in a northern province, so a pre-1970 house there is flagged.
The error direction is deliberate — a needless €400 survey is a far better
outcome than a missed €100k foundation.

### Deliberately not scored

| Field | Why |
|---|---|
| `subsidence_mm_per_year` | No national open service reachable — the Bodemdalingskaart portal and the PDOK path both 404. |
| `contamination_status` | Every published Bodemloket endpoint is dead or 404s. |
| `flood_depth_m` | The national INSPIRE layer carries a bare "flood" category, no depth or return period. |
| `flood_hazard_area` | Present in the payload and raised as an **info** flag, but kept out of the number: Apeldoorn (sandy, inland) returns `True` while Amsterdam (below sea level) returns `False`. Too patchy to weigh. |

The model fields are retained so a future source drops in without a schema
change. They stay `None` rather than defaulting to zero, so nothing reads as a
measured value when it was never measured.

### WMS sampling resolution is not incidental

GeoServer derives a scale denominator from the bounding box and image size of a
GetFeatureInfo request, and that denominator decides both how a raster is
resampled and — for scale-dependent vector layers — **which aggregation level is
served at all**. Measured on `lbm3:score24_schaalafhankelijk`:

| Scale denominator | Aggregation served |
|---|---|
| < ~30,000 | 100 m grid |
| ~60,000 | wijk |
| ~140,000 | gemeente |

A coarse request returns a coarser answer that looks entirely valid. The client
defaults (50 m half-extent at 256 px, about 1:1,400) sit well inside the grid
band; `WMSClient.scale_denominator()` exists so this can be asserted, and two
tests do.

### Reading the noise figures

Raster layers return **0 where a source is not mapped**, which is the absence
of a source, not a 0 dB reading — genuine low values (14 dB rail at night) do
occur, so only exact zero is treated as absent. A provider result that is `OK`
with `sources_mapped == 0` therefore means *genuinely quiet*, and the scorer
reads it as a 10 rather than imputing. A dead WMS is a separate case and comes
back `MISSING`.

Noise is scored with `linear_score`, not `log_score`: the decibel scale is
already logarithmic, so interpolating linearly in dB *is* interpolating
logarithmically in acoustic energy. Taking the log twice would be wrong.

### Reading the crime figures

Crime is registered where it **happens**; the denominator is who **lives**
there. Nightlife and retail buurten therefore look far worse than a resident
experiences — a few hundred residents absorb the recorded crime of tens of
thousands of daily visitors. The safety dimension weights residential burglary
highest (35%) partly because it is least distorted by this. Treat centre-of-town
figures as an upper bound, not a resident's exposure.

### CBS StatLine quirks (verified against 86165NED)

Pinned in `providers/cbs_odata.py` so the Politie provider need not rediscover
them — each one fails *silently*, which is what makes them expensive:

| Behaviour | Consequence |
|-----------|-------------|
| PDOK WFS attribute filters | `cql_filter` is accepted and **ignored** — asking BAG for one dwelling in Amsterdam returned three in Appingedam. Use the standards-track OGC `filter` parameter, and verify the value that comes back. |
| Key padding differs **per dimension and per table** | `WijkenEnBuurten` pads to 10, `SoortMisdrijf` to 6, and `RegioS` in 83625NED not at all. Each is pinned beside the table it belongs to. |
| Dimension keys are space-padded, to a **different width per dimension** | `WijkenEnBuurten` is 10, `SoortMisdrijf` is 6; a wrong width returns `[]` with a 200 |
| Range filters (`ge`/`lt`) on `WijkenEnBuurten` are ignored | Server returns unfiltered head rows, also with a 200 |
| ...but they *do* work on `Perioden` | Verified per column; do not generalise either way |
| `$skip` is unsupported | HTTP 500; partition with `startswith` instead of paging |
| `$top` caps at 5000 | Larger values return HTTP 500 |
| Politie tables live on a different host | `dataderden.cbs.nl`, not `opendata.cbs.nl` |

### Other endpoint traps

| Service | Trap |
|---------|------|
| Leefbaarometer | The catalogue advertises `geo.leefbaarometer.nl/wms`, which answers **200 with an empty body**. The working path is `/geoserver/wms`. |
| Leefbaarometer classes | The 1-9 legend order is not alphabetical or intuitive: `Zwak` (4) ranks *above* `Onvoldoende` (3). Taken from the published legend, not guessed. |
| PDOK WFS | `cql_filter` is **silently ignored** — filtering on `pc6='1015AA'` returns a 200 carrying a feature from Vorden instead. Spatial filtering must go through `bbox`, with the CRS URI spelled out. |
| DUO CKAN | The verdict resource URL returned by the API points at an internal host (`beheer-ggm-ckan-prd.apps.prd.duo.rijksapps.nl`) that does not resolve publicly. Rewrite it onto `onderwijsdata.duo.nl`. |

Scoring anchors are calibrated on measured national distributions rather than
intuition — households-with-children across all 14,729 buurten, and crime rates
across the 10,941 buurten with 200+ residents. Crime is scored in **log space**
against the national rate: the raw distribution runs from 7.7 to 2450 per 1000,
so a linear scale would put ~90% of the country in the top third of the range.
See `services/scoring/weights.py`.

## Scraping policy

Compliance is enforced in code, not by convention. Every scraper fetch passes
through `app.core.robots.RobotsGate`, which fails **closed**: it refuses when
robots.txt disallows the path, when the response is not parseable robots
directives, and when it cannot be fetched at all. An unreachable policy is not
permission.

| Source | Status |
|---|---|
| **Huispedia** | Implemented. Its robots.txt permits crawlers on listing content (blocking only tracking and contact paths), and it publishes both a sitemap index and schema.org JSON-LD. |
| **Funda** | **Not scraped, deliberately.** `funda.nl/robots.txt` returns 200 with ~14 KB of HTML — an anti-bot interstitial, not a policy file. Combined with their terms, that is the site declining automated traffic. Scraping it would mean defeating that detection. `app/scrapers/funda.py` documents the legitimate routes instead: Funda's own e-mail alerts, a licensed Partners feed, or the listing broker's own site. |

The agent identifies itself honestly, with one stable User-Agent. An earlier
draft rotated browser User-Agents to avoid being blocked; that was removed.
Rotation only helps against a site that does not want you, and this agent does
not visit those.

### Scoring a Funda listing without fetching Funda

A Funda listing URL already carries the address:

```
https://www.funda.nl/detail/koop/amsterdam/huis-singel-30/43829102/
                                  ^city     ^type ^street ^nr  ^id
```

`app/scrapers/funda_url.py` parses that string — the site is never requested —
and PDOK resolves street + number + city to a postcode, coordinates and CBS
area codes, which is everything the six enrichment layers need:

```bash
uv run woonagent-score https://www.funda.nl/detail/koop/amsterdam/huis-singel-30/43829102/ --year 1890
```

The UI has a URL box too, and `POST /score-url` does the same over HTTP.

The URL gives only the address, but most of what the page would have told you
is in the national registries anyway, and those are authoritative:

| Fact | Source | Key needed |
|---|---|---|
| Construction year | **BAG** (national building registry, via PDOK) | no |
| Floor area | **BAG** (per dwelling, via its BAG id) | no |
| Energy label | **EP-Online** (RVO's register) | free key |
| Asking price | only the listing | — pass `--price` |

So `--year` is now optional: BAG supplies it, and more accurately than a
listing often does. Singel 30 in Amsterdam turns out to be **1730**, not the
1890 that had been guessed — which is exactly the kind of thing the pile-rot
check depends on.

The registry fills *gaps only*: anything the listing states wins, because the
seller knows about the converted attic and the registry does not. Set
`WOONAGENT_EPONLINE_API_KEY` for energy labels; without it they are absent
rather than an error.

Two subtleties worth knowing.

**Which year.** For a building with several dwellings BAG records a year on the
*pand* (the structure) and another on each *verblijfsobject* (the unit) — Singel
30 reads 1730 and 1702. The foundation belongs to the structure, so the pand
year governs risk.

**Which dwelling.** A spatial query cannot tell one flat from another inside a
shared footprint, so the dwelling is fetched by its BAG id, which PDOK returns
as `adresseerbaarobject_id`. That makes the house-number *addition* decisive:
Cruquiuskade 289 is 183 m², 289-A is 37 m² and 289-B is 76 m². Without the id
the area is only trusted when the building holds exactly one dwelling.

Note that BAG's `oppervlakte` is *gebruiksoppervlakte* under NEN 2580 — usually
the figure a listing advertises as woonoppervlakte, but not by definition. The
289-B flat reads 76 m² in BAG against 74 m² on the listing; expect agreement
within a few m², not exactly.

**The match is verified, not trusted.** Locatieserver is fuzzy and always
returns something: `huis-te-koop-mooi-1` resolves to a real street in Leiden
with relevance 13.1, *higher* than a correct match for Singel 30, so a score
threshold is no guard at all. Instead the returned street and city are compared
against what the URL said, and a disagreement is an error rather than a
confidently wrong house.

### Why sitemaps and JSON-LD rather than page scraping

Huispedia publishes three interfaces meant to be read by machines, and the
scraper uses all three: `properties-listed-*.xml.gz` sitemaps carry a `lastmod`
per property (that is how "newly listed" is decided — no search pagination);
the URL shape `/{city}/{pc6}/{street}/{number}` yields the postcode before any
page is fetched; and each page carries `SingleFamilyResidence` + `Product`
JSON-LD with address, coordinates, floor size, rooms and price.

Only the construction year needs markup parsing — it sits in a feature list
rather than the JSON-LD — and it matters enough to the foundation-risk layer to
be worth the fragility. Everything else survives a redesign of the page.
