"""Render a ScoredProperty as a Telegram MarkdownV2 card.

Formatting is deliberately separate from delivery: the same card renders into a
push notification today and a mobile-app detail view in Phase 2.

Three constraints shape what follows.

* **MarkdownV2 is unforgiving.** Telegram rejects the whole message with a 400
  if a single reserved character is left unescaped, so every interpolated value
  goes through :func:`escape_md` and no caller writes raw text into the body.
* **It is read on a phone, at a glance.** The score and the flags come first;
  the supporting figures are one short line per layer, not a data dump.
* **Missing data is absent, never "None".** Every section is guarded on the
  provider result being usable, so a degraded run produces a shorter card
  rather than a card full of blanks.
"""
from __future__ import annotations

import re
from typing import Optional

from app.models.enrichment import EnrichmentBundle
from app.models.score import HolisticScore, ScoredProperty

_MDV2_SPECIALS = r"_*[]()~`>#+-=|{}.!"
_MDV2_LINK_SPECIALS = r")\\"

BAR_WIDTH = 10
MAX_FLAGS = 4
SEVERITY_ICON = {"critical": "🔴", "warn": "🟠", "info": "🔵"}

#: School positions come from geocoding a postcode, so they are good to roughly
#: 50-100 m. Printing "0 m" would claim a precision the data does not have.
SCHOOL_DISTANCE_FLOOR_M = 100

#: The soil provider labels risk in Dutch, since that is the vocabulary of the
#: source. The card is English, so it maps the language-neutral ordinal rather
#: than the label — no lookup by Dutch string, nothing to drift.
FOUNDATION_RISK_LABEL = {0: "none", 1: "low", 2: "moderate", 3: "high", 4: "very high"}

#: The card is English throughout, matching RiskFlag.message and the rest of
#: the codebase. Translating the labels but not the flag text — which carries
#: the numbers a reader actually acts on — would be worse than either language
#: alone. To localise properly, give RiskFlag structured params so its message
#: can be re-templated, rather than translating around it here.
DIMENSION_LABEL = {
    "physical_safety": "Safety",
    "peer_family_concentration": "Families",
    "education_quality_access": "Schools",
    "environmental_health": "Environment",
    "structural_security": "Foundation",
}


def escape_md(text: object) -> str:
    """Escape MarkdownV2 reserved characters. Telegram 400s otherwise."""
    return re.sub(f"([{re.escape(_MDV2_SPECIALS)}])", r"\\\1", str(text))


def escape_link(url: str) -> str:
    """Inside a ``(...)`` link target only ')' and '\\' need escaping."""
    return re.sub(f"([{re.escape(_MDV2_LINK_SPECIALS)}])", r"\\\1", url)


def score_bar(score: float, width: int = BAR_WIDTH) -> str:
    filled = max(0, min(width, int(round(score * width / 10))))
    return "█" * filled + "░" * (width - filled)


def _euros(amount: Optional[int]) -> str:
    return f"€{amount:,}".replace(",", ".") if amount else "n/a"


def render_card(item: ScoredProperty) -> str:
    """Build the full alert body. Pure string work — no I/O."""
    lines: list[str] = []
    lines += _header(item)
    lines += _facts(item)
    if item.score:
        lines += _breakdown(item.score)
    if item.enrichment:
        lines += _details(item.enrichment)
    if item.score:
        lines += _flags(item.score)
    lines += _footer(item)
    return "\n".join(lines).strip()


# -- sections ----------------------------------------------------------------


def _header(item: ScoredProperty) -> list[str]:
    out = [f"*🏡 {escape_md(item.listing.address)}*"]
    geo = item.enrichment.geo if item.enrichment else None
    if geo and (geo.buurtnaam or geo.gemeentenaam):
        where = " · ".join(p for p in (geo.buurtnaam, geo.gemeentenaam) if p)
        out.append(f"_{escape_md(where)}_")
    if item.score:
        out.append(
            f"`{score_bar(item.score.total_score)}` *{escape_md(f'{item.score.total_score:.1f}')}/10*"
        )
    return out + [""]


def _facts(item: ScoredProperty) -> list[str]:
    listing = item.listing
    price = _euros(listing.price_eur)
    if listing.price_per_m2:
        price += f" ({_euros(int(listing.price_per_m2))}/m²)"
    return [
        f"💰 {escape_md(price)}",
        f"📐 {escape_md(listing.living_area_m2 or '?')} m² · "
        f"🏗 {escape_md(listing.construction_year or '?')}",
        "",
    ]


def _breakdown(score: HolisticScore) -> list[str]:
    """One line per dimension, so the headline number is auditable at a glance."""
    out = ["*Score*"]
    for dimension in score.dimensions:
        label = DIMENSION_LABEL.get(dimension.dimension.value, dimension.dimension.value)
        # An imputed dimension is marked, not silently averaged into the total.
        suffix = " _\\(no data\\)_" if dimension.imputed else ""
        out.append(
            f"`{score_bar(dimension.score, 6)}` {escape_md(f'{dimension.score:4.1f}')} "
            f"{escape_md(label)}{suffix}"
        )
    if score.data_coverage_pct < 100:
        out.append(
            f"_{escape_md(f'{score.data_coverage_pct:.0f}')}% of data sources available_"
        )
    return out + [""]


def _details(bundle: EnrichmentBundle) -> list[str]:
    """The supporting figures, one short line per layer that resolved."""
    out: list[str] = []

    if bundle.demographics.usable and (d := bundle.demographics.payload):
        bits = []
        if d.households_with_children_pct is not None:
            bits.append(f"{d.households_with_children_pct:.0f}% of households have children")
        if d.children_pct_vs_national:
            bits.append(f"{d.children_pct_vs_national:.2f}× national")
        if bits:
            out.append(f"👨‍👩‍👧 {escape_md(' · '.join(bits))}")

    if bundle.crime.usable and (c := bundle.crime.payload):
        bits = []
        if c.national_average_ratio is not None:
            bits.append(f"crime {c.national_average_ratio:.2f}× national")
        if (burglary := (c.category_ratios or {}).get("burglaries")) is not None:
            bits.append(f"burglary {burglary:.2f}×")
        if c.trend_12m_pct is not None:
            arrow = "↑" if c.trend_12m_pct > 0 else "↓"
            bits.append(f"{arrow}{abs(c.trend_12m_pct):.0f}% y/y")
        if bits:
            out.append(f"🚨 {escape_md(' · '.join(bits))}")

    if bundle.education.usable and (e := bundle.education.payload):
        bits = []
        if e.nearest_primary_distance_m is not None:
            distance = e.nearest_primary_distance_m
            bits.append(
                f"nearest school <{SCHOOL_DISTANCE_FLOOR_M} m"
                if distance < SCHOOL_DISTANCE_FLOOR_M
                else f"nearest school {distance} m"
            )
        if e.primary_schools_within_1km:
            bits.append(f"{e.primary_schools_within_1km} within 1 km")
        if bits:
            out.append(f"🎒 {escape_md(' · '.join(bits))}")

    if bundle.noise.usable and (n := bundle.noise.payload):
        if n.cumulative_lden_db is not None:
            bits = [f"{n.cumulative_lden_db:.0f} dB Lden"]
            if n.road_lnight_db is not None:
                bits.append(f"{n.road_lnight_db:.0f} dB at night")
            out.append(f"🔊 {escape_md(' · '.join(bits))}")
        elif n.sources_mapped == 0:
            out.append("🔊 " + escape_md("no mapped noise source"))

    if bundle.leefbaarometer.usable and (lb := bundle.leefbaarometer.payload):
        if lb.klasse:
            out.append(f"🌳 {escape_md(f'Leefbaarometer: {lb.klasse}')}")

    if bundle.soil.usable and (s := bundle.soil.payload):
        bits = []
        risk = FOUNDATION_RISK_LABEL.get(s.foundation_risk_ordinal)
        if risk:
            bits.append(f"foundation risk {risk}")
        if s.soil_type and s.soil_type.lower() != "niet indeelbaar":
            bits.append(s.soil_type.lower())
        if bits:
            out.append(f"🧱 {escape_md(' · '.join(bits))}")

    return out + [""] if out else out


def _flags(score: HolisticScore) -> list[str]:
    if not score.risk_flags:
        return []
    # Worst first, and capped: a card that scrolls is a card nobody reads.
    ranked = sorted(
        score.risk_flags,
        key=lambda f: {"critical": 0, "warn": 1, "info": 2}.get(f.severity, 3),
    )
    out = ["*⚠️ Watch out*"]
    for flag in ranked[:MAX_FLAGS]:
        icon = SEVERITY_ICON.get(flag.severity, "•")
        out.append(f"{icon} {escape_md(flag.message)}")
    if len(ranked) > MAX_FLAGS:
        out.append(escape_md(f"… and {len(ranked) - MAX_FLAGS} more"))
    return out + [""]


def _footer(item: ScoredProperty) -> list[str]:
    return [f"[View listing]({escape_link(item.listing.url)})"]
