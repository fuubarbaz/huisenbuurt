"""Scoring engine tests. Pure functions, no I/O."""
from __future__ import annotations

import pytest

from app.models.enrichment import (
    AirQualityData,
    CrimeStats,
    DemographicsData,
    EnrichmentBundle,
    LeefbaarometerData,
    NoiseData,
    ProviderResult,
    ProviderStatus,
    SoilRiskData,
)
from app.models.enrichment import EducationData
from app.services.scoring import weights as W
from app.services.scoring.scoring_engine import ScoringEngine, linear_score, log_score


def bundle(**layers) -> EnrichmentBundle:
    def empty(name):
        return ProviderResult(provider=name, status=ProviderStatus.MISSING)

    base = {n: empty(n) for n in
            EnrichmentBundle.LAYER_NAMES}
    base.update(layers)
    return EnrichmentBundle(property_id="x", **base)


def ok(name, payload):
    return ProviderResult(provider=name, status=ProviderStatus.OK, payload=payload)


# -- the scales --------------------------------------------------------------


def test_linear_score_endpoints_and_clamping():
    assert linear_score(10, best=10, worst=0) == 10.0
    assert linear_score(0, best=10, worst=0) == 0.0
    assert linear_score(5, best=10, worst=0) == pytest.approx(5.0)
    assert linear_score(99, best=10, worst=0) == 10.0      # clamped
    assert linear_score(-99, best=10, worst=0) == 0.0      # clamped


def test_log_score_puts_the_geometric_midpoint_at_five():
    # Inverted range: a LOWER ratio is better, as with crime.
    best, worst = 0.1, 10.0
    assert log_score(best, best, worst) == pytest.approx(10.0)
    assert log_score(worst, best, worst) == pytest.approx(0.0)
    assert log_score(1.0, best, worst) == pytest.approx(5.0)  # geometric mean


def test_log_score_beats_linear_on_skewed_data():
    """The point of the log scale: keep resolution in the crowded low range."""
    best, worst = W.CRIME_RATIO_BEST, W.CRIME_RATIO_WORST
    median, p75 = 0.57, 1.02
    # Under a log scale the median-to-p75 step is a full point or more...
    assert log_score(median, best, worst) - log_score(p75, best, worst) > 1.0
    # ...and the median lands mid-scale rather than near the top.
    assert 5.0 < log_score(median, best, worst) < 6.5


def test_log_score_handles_zero_and_degenerate_ranges():
    assert log_score(0.0, 0.1, 10.0) == 10.0     # zero crime is the best case
    assert log_score(5.0, 1.0, 1.0) == 5.0       # no range -> neutral


# -- dimensions --------------------------------------------------------------


def test_safety_weights_burglary_above_violence():
    """Same ratio everywhere except one group; burglary should hurt more."""
    engine = ScoringEngine()

    def safety_with(ratios):
        b = bundle(crime=ok("politie", CrimeStats(
            area_level="buurt", category_ratios=ratios, national_average_ratio=1.0)))
        return next(d for d in engine.score(b).dimensions
                    if d.dimension.value == "physical_safety").score

    flat = {g: 1.0 for g in W.CRIME_GROUP_WEIGHTS}
    bad_burglary = {**flat, "burglaries": 4.0}
    bad_violence = {**flat, "violent": 4.0}

    assert safety_with(bad_burglary) < safety_with(bad_violence)


def test_safety_falls_back_to_total_when_groups_are_empty():
    engine = ScoringEngine()
    b = bundle(crime=ok("politie", CrimeStats(
        area_level="buurt", category_ratios={}, national_average_ratio=3.0)))
    dim = next(d for d in engine.score(b).dimensions if d.dimension.value == "physical_safety")

    assert not dim.imputed
    assert "all registered crime" in dim.drivers[0]


def test_trend_nudges_but_never_dominates():
    engine = ScoringEngine()

    def safety_with(trend):
        b = bundle(crime=ok("politie", CrimeStats(
            area_level="buurt", category_ratios={g: 1.0 for g in W.CRIME_GROUP_WEIGHTS},
            trend_12m_pct=trend)))
        return next(d for d in engine.score(b).dimensions
                    if d.dimension.value == "physical_safety").score

    improving, worsening = safety_with(-40.0), safety_with(40.0)
    assert improving > worsening
    # A huge swing is still capped at the configured adjustment, both sides.
    assert improving - worsening <= 2 * W.CRIME_TREND_MAX_ADJUSTMENT + 1e-9


def test_wider_area_lowers_confidence_not_score():
    engine = ScoringEngine()
    ratios = {g: 1.0 for g in W.CRIME_GROUP_WEIGHTS}

    def dim_for(level):
        b = bundle(crime=ok("politie", CrimeStats(area_level=level, category_ratios=ratios)))
        return next(d for d in engine.score(b).dimensions if d.dimension.value == "physical_safety")

    buurt, gemeente = dim_for("buurt"), dim_for("gemeente")
    assert buurt.score == gemeente.score
    assert gemeente.confidence < buurt.confidence


# -- composite ---------------------------------------------------------------


def test_missing_layers_are_imputed_and_flagged():
    engine = ScoringEngine()
    score = engine.score(bundle())

    assert all(d.imputed for d in score.dimensions)
    assert score.confidence == 0.0
    assert score.data_coverage_pct == 0.0
    assert any(f.code == "LOW_DATA_COVERAGE" for f in score.risk_flags)
    # An all-imputed score still sits in range rather than collapsing to 0.
    assert 1.0 <= score.total_score <= 10.0


def test_weights_sum_to_one_across_dimensions():
    score = ScoringEngine().score(bundle())
    assert sum(d.weight for d in score.dimensions) == pytest.approx(1.0)


def test_confidence_reflects_which_layers_resolved():
    engine = ScoringEngine()
    b = bundle(
        crime=ok("politie", CrimeStats(area_level="buurt", category_ratios={"burglaries": 1.0})),
        demographics=ok("cbs", DemographicsData(
            area_level="buurt", households_with_children_pct=30.0)),
    )
    score = engine.score(b)

    # Safety (0.25) + family (0.20) resolved at full confidence.
    assert score.confidence == pytest.approx(0.45, abs=0.01)
    # Two of the seven layers. Derived rather than written out, so adding an
    # eighth is a one-line change in the model and not a test failure here.
    assert score.data_coverage_pct == pytest.approx(
        200 / len(EnrichmentBundle.LAYER_NAMES), abs=0.1)


# -- environment -------------------------------------------------------------


def env_dim(**layers):
    engine = ScoringEngine()
    return next(d for d in engine.score(bundle(**layers)).dimensions
                if d.dimension.value == "environmental_health")


def test_quiet_address_scores_top_and_is_not_imputed():
    """sources_mapped == 0 on a usable result means silent, not unknown."""
    dim = env_dim(noise=ok("rivm", NoiseData(sources_mapped=0)))

    assert not dim.imputed
    assert dim.score == 10.0
    assert "no mapped noise source" in dim.drivers[0]


def test_missing_noise_layer_is_imputed_rather_than_scored_as_quiet():
    dim = env_dim()   # noise result is MISSING, not OK
    assert dim.imputed


def test_louder_scores_lower_across_the_anchor_range():
    scores = [
        env_dim(noise=ok("rivm", NoiseData(cumulative_lden_db=db, sources_mapped=1))).score
        for db in (40.0, 50.0, 60.0, 75.0)
    ]
    assert scores == sorted(scores, reverse=True)
    assert scores[0] == 10.0    # below the good anchor, clamped
    assert scores[-1] == 0.0    # above the bad anchor, clamped


def test_noise_alone_scores_the_dimension_at_partial_confidence():
    dim = env_dim(noise=ok("rivm", NoiseData(cumulative_lden_db=55.0, sources_mapped=1)))

    assert not dim.imputed
    assert dim.confidence == pytest.approx(W.ENVIRONMENT_NOISE_SHARE)


def test_two_of_three_parts_gives_partial_confidence():
    """Air quality is the third part of this dimension, so noise plus
    livability no longer accounts for all of it."""
    dim = env_dim(
        noise=ok("rivm", NoiseData(cumulative_lden_db=55.0, sources_mapped=1)),
        leefbaarometer=ok("lbm", LeefbaarometerData(klasse_ordinal=7, klasse="Goed")),
    )
    assert dim.confidence == pytest.approx(
        W.ENVIRONMENT_NOISE_SHARE + W.ENVIRONMENT_LIVABILITY_SHARE)
    assert len(dim.drivers) == 2


def test_all_three_parts_present_gives_full_confidence():
    dim = env_dim(
        noise=ok("rivm", NoiseData(cumulative_lden_db=55.0, sources_mapped=1)),
        air=ok("rivm_air", AirQualityData(no2_ug_m3=16.0, pm25_ug_m3=9.0, pm10_ug_m3=17.0)),
        leefbaarometer=ok("lbm", LeefbaarometerData(klasse_ordinal=7, klasse="Goed")),
    )
    assert dim.confidence == pytest.approx(1.0)
    assert len(dim.drivers) == 3


def test_environment_uses_physical_subscore_not_the_composite():
    """The composite folds in safety, which the safety dimension already scores.

    A cell with an excellent composite but a poor physical environment must
    score on the physical part, or crime is counted twice.
    """
    dim = env_dim(leefbaarometer=ok("lbm", LeefbaarometerData(
        klasse_ordinal=9, klasse="Uitstekend",
        dimension_classes={"fysieke_omgeving": 2, "veiligheid": 9},
    )))

    assert dim.score == pytest.approx(linear_score(2, best=9, worst=1))
    assert "physical environment class 2" in dim.drivers[0]


def test_environment_falls_back_to_composite_without_subscores():
    dim = env_dim(leefbaarometer=ok("lbm", LeefbaarometerData(
        klasse_ordinal=7, klasse="Goed", dimension_classes={})))

    assert dim.score == pytest.approx(linear_score(7, best=9, worst=1))
    assert "overall class" in dim.drivers[0]


# -- WHO noise flags ---------------------------------------------------------


def flags_for(noise: NoiseData):
    return {f.code: f for f in ScoringEngine().score(bundle(noise=ok("rivm", noise))).risk_flags}


def test_each_source_is_flagged_against_its_own_guideline():
    # 50 dB is under the road guideline (53) but over the aviation one (45).
    flags = flags_for(NoiseData(road_lden_db=50.0, aviation_lden_db=50.0, sources_mapped=2))

    assert "NOISE_ROAD_ABOVE_WHO" not in flags
    assert "NOISE_AVIATION_ABOVE_WHO" in flags


def test_large_exceedance_escalates_to_critical():
    mild = flags_for(NoiseData(road_lden_db=58.0, sources_mapped=1))
    severe = flags_for(NoiseData(road_lden_db=72.0, sources_mapped=1))

    assert mild["NOISE_ROAD_ABOVE_WHO"].severity == "warn"
    assert severe["NOISE_ROAD_ABOVE_WHO"].severity == "critical"


def test_night_noise_is_flagged_separately_from_lden():
    flags = flags_for(NoiseData(road_lden_db=45.0, road_lnight_db=55.0, sources_mapped=1))

    assert "NOISE_ROAD_ABOVE_WHO" not in flags      # Lden is fine
    assert "NIGHT_NOISE_ROAD_ABOVE_WHO" in flags    # nights are not


def test_quiet_address_raises_no_noise_flags():
    assert not [c for c in flags_for(NoiseData(sources_mapped=0)) if c.startswith(("NOISE_", "NIGHT_"))]


# -- structural security -----------------------------------------------------


def struct_dim(soil: SoilRiskData):
    engine = ScoringEngine()
    return next(d for d in engine.score(bundle(soil=ok("soil", soil))).dimensions
                if d.dimension.value == "structural_security")


def test_foundation_ordinal_drives_the_score_monotonically():
    scores = [struct_dim(SoilRiskData(foundation_risk_ordinal=n)).score for n in range(5)]

    assert scores == sorted(scores, reverse=True)
    assert scores[0] == 10.0 and scores[4] == 0.0


def test_inferred_construction_year_lowers_confidence_only():
    known = struct_dim(SoilRiskData(foundation_risk_ordinal=3, construction_year_known=True))
    inferred = struct_dim(SoilRiskData(
        foundation_risk_ordinal=3, construction_year_known=False, area_pre_1970_pct=83.3))

    assert known.score == inferred.score
    assert inferred.confidence < known.confidence
    assert "construction year unknown" in inferred.drivers[-1]


def test_no_classification_is_imputed():
    assert struct_dim(SoilRiskData(foundation_risk_ordinal=None)).imputed


def test_flood_probability_now_moves_the_score():
    """Superseded the old EU Floods Directive boolean (Apeldoorn: inside the
    hazard area; Amsterdam: outside it — backwards), which was surfaced only
    as a flag. RIVM's return-period class is trustworthy enough to score."""
    dry = struct_dim(SoilRiskData(foundation_risk_ordinal=1, flood_risk_ordinal=1))
    wet = struct_dim(SoilRiskData(foundation_risk_ordinal=1, flood_risk_ordinal=5))

    assert dry.score > wet.score


def test_foundation_alone_is_renormalised_to_full_weight():
    """A missing flood sample must not drag the dimension toward some assumed
    midpoint for the missing part — the foundation share must be scaled up to
    the dimension's FULL weight, so the result is exactly the raw foundation
    score, not a blend with a phantom neutral flood value."""
    foundation_only = struct_dim(SoilRiskData(foundation_risk_ordinal=1, flood_risk_ordinal=None))
    raw_foundation_alone = linear_score(
        1, best=W.FOUNDATION_ORDINAL_BEST, worst=W.FOUNDATION_ORDINAL_WORST)

    assert foundation_only.score == pytest.approx(raw_foundation_alone, abs=0.01)


def test_a_missing_flood_sample_costs_confidence():
    with_flood = struct_dim(SoilRiskData(foundation_risk_ordinal=1, flood_risk_ordinal=2))
    without_flood = struct_dim(SoilRiskData(foundation_risk_ordinal=1, flood_risk_ordinal=None))

    assert without_flood.confidence < with_flood.confidence


# -- soil flags --------------------------------------------------------------


def soil_flags(soil: SoilRiskData):
    return {f.code: f for f in ScoringEngine().score(bundle(soil=ok("soil", soil))).risk_flags}


def test_paalrot_is_critical_and_names_the_remedy():
    flags = soil_flags(SoilRiskData(
        paalrot_risk=True, foundation_risk_ordinal=3,
        soil_vulnerability="stedelijk gebied", construction_year_known=True))

    assert flags["PAALROT_RISK"].severity == "critical"
    assert "funderingsonderzoek" in flags["PAALROT_RISK"].message


def test_inferred_era_is_hedged_in_the_flag_text():
    flags = soil_flags(SoilRiskData(
        paalrot_risk=True, foundation_risk_ordinal=3, construction_year_known=False))

    assert "probably pre-1970" in flags["PAALROT_RISK"].message


def test_high_risk_without_paalrot_is_a_warning_not_a_critical():
    flags = soil_flags(SoilRiskData(paalrot_risk=False, foundation_risk_ordinal=3,
                                    foundation_risk_class="hoog"))

    assert "PAALROT_RISK" not in flags
    assert flags["HIGH_FOUNDATION_RISK"].severity == "warn"


def test_elevated_flood_probability_is_flagged():
    flags = soil_flags(SoilRiskData(foundation_risk_ordinal=0, flood_risk_ordinal=4,
                                    flood_risk_class="1x per 100 jaar"))

    assert flags["ELEVATED_FLOOD_PROBABILITY"].severity == "warn"
    assert "1x per 100 jaar" in flags["ELEVATED_FLOOD_PROBABILITY"].message


def test_the_worst_flood_class_is_critical_not_just_a_warning():
    flags = soil_flags(SoilRiskData(foundation_risk_ordinal=0, flood_risk_ordinal=5,
                                    flood_risk_class="1x per 10 jaar"))

    assert flags["ELEVATED_FLOOD_PROBABILITY"].severity == "critical"


def test_a_low_flood_class_is_not_flagged_at_all():
    flags = soil_flags(SoilRiskData(foundation_risk_ordinal=0, flood_risk_ordinal=2,
                                    flood_risk_class="1x per 100.000 jaar"))

    assert "ELEVATED_FLOOD_PROBABILITY" not in flags


def test_low_risk_property_raises_no_soil_flags():
    flags = soil_flags(SoilRiskData(foundation_risk_ordinal=0, paalrot_risk=False))

    assert not {c for c in flags if c.startswith(("PAALROT", "HIGH_FOUND", "IN_FLOOD"))}


# -- education ---------------------------------------------------------------


def edu_dim(edu: EducationData):
    engine = ScoringEngine()
    return next(d for d in engine.score(bundle(education=ok("duo", edu))).dimensions
                if d.dimension.value == "education_quality_access")


def test_closer_school_scores_higher():
    scores = [edu_dim(EducationData(nearest_primary_distance_m=d,
                                    primary_schools_within_1km=2)).score
              for d in (200, 500, 900, 3000)]
    assert scores == sorted(scores, reverse=True)


def test_national_median_distance_lands_mid_scale():
    """900 m is the national median; it should not score like a bad address."""
    dim = edu_dim(EducationData(nearest_primary_distance_m=900, primary_schools_within_1km=2))
    assert 3.5 < dim.score < 7.0


def test_choice_within_a_kilometre_counts():
    alone = edu_dim(EducationData(nearest_primary_distance_m=500, primary_schools_within_1km=0))
    spoilt = edu_dim(EducationData(nearest_primary_distance_m=500, primary_schools_within_1km=4))
    assert spoilt.score > alone.score


def test_poor_rating_penalty_is_capped():
    """Ratings prompt a look; they never dominate a current, measured distance."""
    clean = edu_dim(EducationData(nearest_primary_distance_m=400, primary_schools_within_1km=3))
    one_bad = edu_dim(EducationData(nearest_primary_distance_m=400, primary_schools_within_1km=3,
                                    poorly_rated_nearby=["A"]))
    many_bad = edu_dim(EducationData(nearest_primary_distance_m=400, primary_schools_within_1km=3,
                                     poorly_rated_nearby=["A", "B", "C", "D", "E"]))

    assert one_bad.score < clean.score
    assert clean.score - many_bad.score <= W.EDUCATION_POOR_RATING_PENALTY + 1e-9


def test_stale_ratings_are_dated_in_the_flag():
    engine = ScoringEngine()
    score = engine.score(bundle(education=ok("duo", EducationData(
        nearest_primary_distance_m=400, primary_schools_within_1km=2,
        poorly_rated_nearby=["De Regenboog"], ratings_as_of="2018-09-01"))))
    flag = next(f for f in score.risk_flags if f.code == "POORLY_RATED_SCHOOL_NEARBY")

    assert "2018-09-01" in flag.message
    assert flag.severity == "info"


def test_ratings_carry_no_share_of_the_blend():
    assert W.EDUCATION_DISTANCE_SHARE + W.EDUCATION_CHOICE_SHARE == pytest.approx(1.0)


def test_no_school_found_is_imputed():
    assert edu_dim(EducationData(nearest_primary_distance_m=None)).imputed
