"""Scoring weights and normalisation thresholds — the tuning surface.

Everything subjective about the score lives in this one file so it can be
adjusted (or A/B'd, or exposed as user preferences in the mobile app) without
touching the engine.
"""
from __future__ import annotations

from app.models.score import ScoreDimension

WEIGHTS: dict[ScoreDimension, float] = {
    ScoreDimension.SAFETY: 0.25,
    ScoreDimension.FAMILY: 0.20,
    ScoreDimension.EDUCATION: 0.20,
    ScoreDimension.ENVIRONMENT: 0.20,
    ScoreDimension.STRUCTURAL: 0.15,
}
assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9, "weights must sum to 1.0"


class InvalidWeights(ValueError):
    """The caller asked for a weighting that cannot produce a score."""


def normalise(supplied: dict | None) -> dict[ScoreDimension, float]:
    """Turn whatever a caller sent into a valid weighting that sums to 1.0.

    Buyers do not agree about what matters — a family with small children and a
    couple buying a city flat are reading the same five numbers for different
    reasons — so the weighting is a preference, not a constant. This is the one
    place that preference is validated.

    Three deliberate choices:

    * **Unspecified dimensions keep their default.** Someone who only cares
      that schools should count double should not have to restate the other
      four, and sending a partial dict must not silently zero them.
    * **The result is renormalised to 1.0.** A slider UI cannot be expected to
      produce a set that already sums to one, and the composite is only on the
      1-10 scale if it does.
    * **All-zero is refused rather than defaulted.** It is the one input where
      guessing the intent would be wrong: silently substituting the defaults
      would return a confident number the caller never asked for.
    """
    if not supplied:
        return dict(WEIGHTS)

    merged = dict(WEIGHTS)
    for key, value in supplied.items():
        try:
            dimension = ScoreDimension(key)
        except ValueError as exc:
            raise InvalidWeights(
                f"unknown dimension {key!r}; expected one of "
                f"{', '.join(d.value for d in ScoreDimension)}"
            ) from exc
        if value is None:
            continue
        weight = float(value)
        if weight < 0 or weight != weight:          # NaN never equals itself
            raise InvalidWeights(f"weight for {key!r} must be zero or positive")
        merged[dimension] = weight

    total = sum(merged.values())
    if total <= 0:
        raise InvalidWeights("at least one dimension must carry a non-zero weight")
    return {dimension: weight / total for dimension, weight in merged.items()}

# Neutral fallback for a missing dimension: assume average, and flag low
# confidence rather than silently rewarding or punishing missing data.
IMPUTED_SCORE = 5.5

# --- normalisation anchors (value -> 0..10, linear between the anchors) ----

# --- crime ----------------------------------------------------------------
# Registered crime is strongly right-skewed across buurten. Measured over all
# 10,941 buurten with >=200 inhabitants (Politie 47018NED, 2025, vs CBS
# population): p5=7.7, p25=15.7, median=25.7, p75=45.5, p90=82.1, p95=128.3,
# p99=346 and a maximum of 2450 crimes per 1000 residents.
#
# Two consequences for scoring:
#  1. Score the *ratio to the national rate* (44.8/1000 in 2025), not the raw
#     rate — it is interpretable ("1.8x national") and comparable per offence.
#  2. Interpolate in LOG space. A linear scale would put ~90% of buurten in the
#     top third of the range and let the extreme tail dictate everything.
#
# Anchors are the p5 and p95 ratios, so the median buurt lands near 5.7/10.
CRIME_RATIO_BEST = 0.17   # p5  — safest 5% of buurten
CRIME_RATIO_WORST = 2.87  # p95 — worst 5% of buurten
CRIME_RATE_NATIONAL_PER_1000 = 44.8

# Within the safety dimension, weighted toward what actually affects a family
# living in the house rather than total registered crime.
CRIME_GROUP_WEIGHTS: dict[str, float] = {
    "burglaries": 0.35,
    "vandalism": 0.25,
    "nuisance": 0.20,
    "violent": 0.20,
}

# A sustained multi-year swing is worth about half a point either way.
CRIME_TREND_MAX_ADJUSTMENT = 0.5
CRIME_TREND_PCT_FOR_MAX = 25.0

# Households with children, anchored on the measured national distribution
# rather than intuition. Sampled from all 14,729 buurten in CBS KWB 2025
# (86165NED): p5=7.8%, p25=26.8%, median=34.3%, p75=41.3%, p90=50.0%.
# Anchoring at p5/p90 puts the median buurt at ~6.3/10 and keeps the ends
# discriminating instead of saturating.
FAMILY_PCT_LOW, FAMILY_PCT_HIGH = 8.0, 50.0
#: Population-weighted national share (the NL00 row) — the comparison baseline.
FAMILY_PCT_NATIONAL = 31.5
#: Unweighted median across buurten; higher than the national share because
#: small family-heavy buurten outnumber large single-person urban ones.
FAMILY_PCT_MEDIAN_BUURT = 34.3

# --- education ------------------------------------------------------------
# Distance to the nearest primary school, in metres. Measured across the
# 13,951 buurten CBS publishes a figure for (KWB 2025, AfstandTotSchool):
# p5=300 m, p25=500 m, median=900 m, p75=1600 m, p90=2500 m.
# Scored with log_score: the distribution is right-skewed, and the difference
# between 300 m and 600 m matters far more to a family than 2000 m vs 2300 m.
SCHOOL_DISTANCE_EXCELLENT_M, SCHOOL_DISTANCE_POOR_M = 300, 2500

# Choice within walking distance. Four or more primary schools within a
# kilometre is effectively unconstrained choice.
SCHOOL_CHOICE_BEST, SCHOOL_CHOICE_WORST = 4, 0

# Split of the education dimension. Ratings are deliberately absent from the
# blend — DUO publishes only a 2018 snapshot in which 81% of primary schools
# are "Voldoende", so they neither rank schools nor describe the present.
# They enter as a capped penalty and a flag instead; see EDUCATION_RATING_*.
EDUCATION_DISTANCE_SHARE = 0.60
EDUCATION_CHOICE_SHARE = 0.40

# A nearby school rated below Voldoende costs at most this much, however many
# there are — it is a prompt to look, not a verdict on the address.
EDUCATION_POOR_RATING_PENALTY = 1.0
EDUCATION_POOR_RATING_PENALTY_EACH = 0.5

# --- noise ----------------------------------------------------------------
# Scored with linear_score, deliberately: the decibel scale is ALREADY
# logarithmic, so linear interpolation in dB is log interpolation in acoustic
# energy. Applying log_score here would take the log twice.
#
# Anchors are health guidance rather than a sampled distribution — for an
# exposure metric the question is "is this harmful", not "is this typical".
# 45 dB Lden sits below every WHO 2018 source guideline; 70 dB is above the
# Dutch maximum exemption value (63 dB) and well into severe annoyance.
NOISE_LDEN_GOOD_DB, NOISE_LDEN_BAD_DB = 45.0, 70.0

#: WHO 2018 Environmental Noise Guidelines, strong recommendations, per source.
#: Exceeding one of these raises a flag naming that source.
WHO_LDEN_GUIDELINE_DB: dict[str, float] = {
    "road": 53.0,
    "rail": 54.0,
    "aviation": 45.0,
}
#: Night-time guidance. Sleep disturbance is the main child-health pathway.
WHO_LNIGHT_GUIDELINE_DB: dict[str, float] = {
    "road": 45.0,
    "rail": 44.0,
    "aviation": 40.0,
}

# --- air quality ----------------------------------------------------------
# WHO 2021 Global Air Quality Guidelines, annual mean, in µg/m³. Deliberately
# not the EU limit values, which are far weaker — EU allows 40 for NO2 against
# WHO's 10, and 25 for PM2.5 against WHO's 5. An Amsterdam address measures
# NO2 16.3 and PM2.5 9.2: comfortably legal, roughly double the health
# guideline. Scoring on the EU number would mark almost every Dutch address
# perfect and tell a buyer nothing.
WHO_ANNUAL_GUIDELINE_UG_M3: dict[str, float] = {
    "no2": 10.0,
    "pm25": 5.0,
    "pm10": 15.0,
}

# Scored on the ratio to the WHO guideline rather than raw µg/m³, so the three
# pollutants are comparable and the number is interpretable ("1.6x the WHO
# guideline"). At the guideline the address scores 10; at three times it
# scores 0. Interpolated linearly: unlike crime these are narrowly distributed
# across the country, so there is no long tail for a log scale to fix.
AIR_RATIO_BEST, AIR_RATIO_WORST = 1.0, 3.0

# PM2.5 carries the largest measured health burden of the three, so it leads.
AIR_POLLUTANT_WEIGHTS: dict[str, float] = {
    "pm25": 0.50,
    "no2": 0.35,
    "pm10": 0.15,
}

# Split of the environmental-health dimension. Re-normalised when a part is
# missing, so noise alone still produces a usable dimension score.
#
# Air quality entering this dimension moved noise from 0.60 to 0.40 and
# livability from 0.40 to 0.25. The reasoning: noise and air are the two
# *measured* exposures at an address, and PM2.5 is the larger health burden of
# the pair, so they should be comparable in weight. Leefbaarometer's physical
# sub-score is a modelled composite rather than a measurement, so it gives way
# to both.
ENVIRONMENT_NOISE_SHARE = 0.40
ENVIRONMENT_AIR_SHARE = 0.35
ENVIRONMENT_LIVABILITY_SHARE = 0.25

# --- structural security --------------------------------------------------
# Foundation risk is an ordinal 0..4 built by the soil provider from ground
# vulnerability and the building's own era; 0 maps to 10/10 and 4 to 0/10.
FOUNDATION_ORDINAL_BEST, FOUNDATION_ORDINAL_WORST = 0, 4

# Confidence when the risk had to be inferred from the area's building-age mix
# because the listing carried no construction year.
STRUCTURAL_CONFIDENCE_KNOWN_YEAR = 1.0
STRUCTURAL_CONFIDENCE_INFERRED_YEAR = 0.6

# RIVM's flood-probability class (1..5, see soil_risk.py for why this
# replaced the old EU Floods Directive hazard-area boolean). Scored linearly:
# a return-period CLASS, not a modelled probability, so there is no long tail
# to correct for the way there is in crime or school distance.
FLOOD_ORDINAL_BEST, FLOOD_ORDINAL_WORST = 1, 5

# Split of the structural-security dimension. Foundation risk leads heavily —
# paalrot repair runs to six figures and is this engine's single most
# consequential number — with flood probability as a real but secondary
# structural exposure. Re-normalised when one part is missing, so a foundation
# reading alone still produces a usable dimension score.
STRUCTURAL_FOUNDATION_SHARE = 0.75
STRUCTURAL_FLOOD_SHARE = 0.25

# Retained for when a national subsidence and flood-depth source is wired; no
# reachable service currently populates either field.
SUBSIDENCE_OK_MM_YR, SUBSIDENCE_SEVERE_MM_YR = 0.5, 8.0
FLOOD_DEPTH_OK_M, FLOOD_DEPTH_SEVERE_M = 0.0, 1.5
