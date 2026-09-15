"""Telegram card tests.

MarkdownV2 is the sharp edge here: Telegram rejects the entire message with a
400 if one reserved character is left unescaped, and that failure only shows up
at delivery time. Most of these tests are about escaping, and about missing
data never rendering as "None".
"""
from __future__ import annotations

import re

from app.models.enrichment import (
    CrimeStats,
    DemographicsData,
    EducationData,
    EnrichmentBundle,
    LeefbaarometerData,
    NoiseData,
    ProviderResult,
    ProviderStatus,
    SoilRiskData,
)
from app.models.property import GeoIdentity, ListingSource, PropertyListing
from app.models.score import (
    DimensionScore,
    HolisticScore,
    RiskFlag,
    ScoreDimension,
    ScoredProperty,
)
from app.notifiers.formatters.markdown_card import (
    MAX_FLAGS,
    escape_link,
    escape_md,
    render_card,
    score_bar,
)

MDV2_SPECIALS = r"_*[]()~`>#+-=|{}.!"


def listing(**kw) -> PropertyListing:
    base = dict(
        property_id="p1", source=ListingSource.HUISPEDIA,
        url="https://huispedia.nl/amsterdam/1018am/cruquiuskade/289-b",
        address="Cruquiuskade 289-B, Amsterdam", postal_code="1018AM",
        house_number="289", price_eur=725_000, living_area_m2=74,
        construction_year=1929,
    )
    base.update(kw)
    return PropertyListing(**base)


def bundle(**layers) -> EnrichmentBundle:
    base = {n: ProviderResult(provider=n, status=ProviderStatus.MISSING)
            for n in EnrichmentBundle.LAYER_NAMES}
    base.update(layers)
    return EnrichmentBundle(
        property_id="p1",
        geo=GeoIdentity(latitude=52.3, longitude=4.9, buurtnaam="Het Funen",
                        gemeentenaam="Amsterdam"),
        **base,
    )


def ok(name, payload):
    return ProviderResult(provider=name, status=ProviderStatus.OK, payload=payload)


def score(total=4.8, flags=(), coverage=100.0) -> HolisticScore:
    return HolisticScore(
        property_id="p1", total_score=total, confidence=1.0, data_coverage_pct=coverage,
        dimensions=[DimensionScore(dimension=d, score=5.0, weight=0.2) for d in ScoreDimension],
        risk_flags=list(flags),
    )


def full_bundle() -> EnrichmentBundle:
    return bundle(
        demographics=ok("cbs", DemographicsData(households_with_children_pct=33.0,
                                                children_pct_vs_national=1.04)),
        crime=ok("politie", CrimeStats(national_average_ratio=1.23,
                                       category_ratios={"burglaries": 1.74},
                                       trend_12m_pct=27.0)),
        education=ok("duo", EducationData(nearest_primary_distance_m=223,
                                          primary_schools_within_1km=9)),
        noise=ok("rivm", NoiseData(cumulative_lden_db=73.0, road_lnight_db=53.0,
                                   sources_mapped=2)),
        leefbaarometer=ok("lbm", LeefbaarometerData(klasse="Zeer goed", klasse_ordinal=8)),
        soil=ok("soil", SoilRiskData(foundation_risk_ordinal=3, soil_type="Laagveengebied")),
    )


def card(**kw) -> str:
    return render_card(ScoredProperty(
        listing=kw.pop("listing", listing()),
        enrichment=kw.pop("enrichment", bundle()),
        score=kw.pop("score", score()),
    ))


# -- escaping ----------------------------------------------------------------


def unescaped_specials(text: str) -> list[str]:
    """Reserved characters that are neither escaped nor deliberate markup."""
    # Strip the markup this card actually emits: [label](target), `code`,
    # *bold* and _italic_.
    stripped = re.sub(r"\[[^\]]*\]\([^)]*\)", "", text)
    stripped = re.sub(r"`[^`]*`", "", stripped)
    stripped = re.sub(r"(?<!\\)[*_]", "", stripped)
    return [ch for i, ch in enumerate(stripped)
            if ch in MDV2_SPECIALS and (i == 0 or stripped[i - 1] != "\\")]


def test_escape_md_covers_every_reserved_character():
    for ch in MDV2_SPECIALS:
        assert escape_md(ch) == f"\\{ch}", ch


def test_escape_md_accepts_non_strings():
    assert escape_md(1929) == "1929"
    assert escape_md(None) == "None"


def test_a_full_card_leaves_no_unescaped_specials():
    text = card(
        enrichment=full_bundle(),
        score=score(flags=[RiskFlag(code="X", severity="critical",
                                    message="Rail noise 73 dB, 19 dB above the guideline.")]),
    )
    assert unescaped_specials(text) == []


def test_address_punctuation_is_escaped():
    text = card(listing=listing(address="Jan van Galenstr. 12-A (achterhuis)!"))
    assert "12\\-A" in text
    assert "\\(achterhuis\\)\\!" in text


def test_link_target_escapes_only_what_matters():
    # Dots and hyphens must survive inside a URL, or the link breaks.
    assert escape_link("https://x.nl/a-b/c.html") == "https://x.nl/a-b/c.html"
    assert escape_link("https://x.nl/a(b)") == "https://x.nl/a(b\\)"


def test_the_listing_url_is_not_over_escaped():
    assert "(https://huispedia.nl/amsterdam/1018am/cruquiuskade/289-b)" in card()


# -- missing data ------------------------------------------------------------


def test_a_bare_card_renders_without_none_anywhere():
    text = render_card(ScoredProperty(
        listing=listing(price_eur=None, living_area_m2=None, construction_year=None)))
    assert "None" not in text
    assert "n/a" in text


def test_degraded_layers_are_absent_rather_than_blank():
    text = card(enrichment=bundle())          # every provider MISSING
    for marker in ("👨‍👩‍👧", "🚨", "🎒", "🔊", "🌳", "🧱"):
        assert marker not in text


def test_a_usable_layer_with_empty_fields_emits_no_line():
    assert "🚨" not in card(enrichment=bundle(crime=ok("politie", CrimeStats())))


def test_partial_coverage_is_stated():
    assert "% of data sources available" in card(score=score(coverage=66.7))
    assert "% of data sources available" not in card(score=score(coverage=100.0))


def test_imputed_dimensions_are_marked_not_hidden():
    s = score()
    s.dimensions[0].imputed = True
    assert "no data" in card(score=s)


# -- content -----------------------------------------------------------------


def test_score_bar_scales_and_clamps():
    assert score_bar(10.0) == "█" * 10
    assert score_bar(0.0) == "░" * 10
    assert score_bar(5.0).count("█") == 5
    assert score_bar(99.0) == "█" * 10        # clamped, not overflowing
    assert score_bar(-5.0) == "░" * 10
    assert len(score_bar(7.0, width=6)) == 6


def test_neighbourhood_appears_under_the_address():
    text = card()
    assert "Het Funen" in text and "Amsterdam" in text


def test_school_distance_is_floored_to_its_real_precision():
    """Positions come from postcode geocoding; "0 m" would overstate them."""
    near = card(enrichment=bundle(education=ok("duo", EducationData(
        nearest_primary_distance_m=0, primary_schools_within_1km=2))))
    assert "nearest school <100 m" in near

    far = card(enrichment=bundle(education=ok("duo", EducationData(
        nearest_primary_distance_m=250, primary_schools_within_1km=2))))
    assert "nearest school 250 m" in far


def test_foundation_risk_is_labelled_in_the_card_language():
    """The provider labels risk in Dutch; the card maps the ordinal instead."""
    text = card(enrichment=bundle(soil=ok("soil", SoilRiskData(
        foundation_risk_ordinal=3, foundation_risk_class="hoog"))))
    assert "foundation risk high" in text
    assert "hoog" not in text


def test_a_quiet_address_says_so_rather_than_omitting_noise():
    text = card(enrichment=bundle(noise=ok("rivm", NoiseData(sources_mapped=0))))
    assert "no mapped noise source" in text


# -- flags -------------------------------------------------------------------


def test_flags_are_ordered_worst_first():
    flags = [
        RiskFlag(code="I", severity="info", message="Informational"),
        RiskFlag(code="C", severity="critical", message="Critical thing"),
        RiskFlag(code="W", severity="warn", message="Warning thing"),
    ]
    text = card(score=score(flags=flags))
    assert text.index("Critical thing") < text.index("Warning thing") < text.index("Informational")


def test_flags_are_capped_with_a_remainder_note():
    flags = [RiskFlag(code=f"F{i}", severity="warn", message=f"Issue number {i}")
             for i in range(MAX_FLAGS + 3)]
    text = card(score=score(flags=flags))

    assert text.count("Issue number") == MAX_FLAGS
    assert "and 3 more" in text


def test_no_flags_means_no_watch_out_section():
    assert "Watch out" not in card(score=score(flags=[]))


# -- size --------------------------------------------------------------------


def test_a_maximal_card_stays_within_the_telegram_limit():
    flags = [RiskFlag(code=f"F{i}", severity="critical", message="x" * 300) for i in range(12)]
    text = card(listing=listing(address="A" * 200), enrichment=full_bundle(),
                score=score(flags=flags))
    assert len(text) < 4096
