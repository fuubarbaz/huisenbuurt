"""Holistic score: EnrichmentBundle -> HolisticScore.

Pure and synchronous by design — no I/O, no clock, no randomness. That makes
it trivially unit-testable and safe to call from a request handler.
"""
from __future__ import annotations

import logging
import math

from app.models.enrichment import EnrichmentBundle
from app.models.score import DimensionScore, HolisticScore, RiskFlag, ScoreDimension
from app.services.scoring import weights as W

log = logging.getLogger(__name__)


def clamp(value: float, low: float = 0.0, high: float = 10.0) -> float:
    return max(low, min(high, value))


def linear_score(value: float, best: float, worst: float) -> float:
    """Map a raw value onto 0..10, where ``best`` scores 10 and ``worst`` 0.

    Handles inverted ranges (worst < best) so both "higher is better" and
    "lower is better" metrics use the same call.
    """
    if best == worst:
        return 5.0
    return clamp(10.0 * (value - worst) / (best - worst))


def log_score(value: float, best: float, worst: float) -> float:
    """Map a value onto 0..10 interpolating in log space.

    For right-skewed quantities (crime rates, noise energy, flood depth) a
    linear scale wastes most of its range on a long thin tail. Working in logs
    treats "twice as bad" as a constant step, which is how these quantities are
    actually experienced.

    ``best`` may be greater or less than ``worst``; both must be > 0.
    """
    if value <= 0:
        return 10.0 if best < worst else 0.0
    if best <= 0 or worst <= 0 or best == worst:
        return 5.0
    lv, lb, lw = math.log10(value), math.log10(best), math.log10(worst)
    return clamp(10.0 * (lv - lw) / (lb - lw))


class ScoringEngine:
    """Compute the weighted 1.0-10.0 composite plus its risk flags.

    The weighting is constructor state rather than a module constant, because
    what "good" means differs by buyer: a family with small children weighs
    schools and safety very differently from a couple buying a city flat. The
    engine stays pure and synchronous either way — re-scoring a bundle under a
    new weighting costs nothing and touches no upstream API, which is what lets
    the UI move a slider and answer instantly.
    """

    def __init__(self, weights: dict[ScoreDimension, float] | None = None) -> None:
        #: Already normalised to sum 1.0. Pass ``weights.normalise(...)`` output
        #: here; the default is the calibrated weighting in ``weights.py``.
        self.weights = dict(weights) if weights else dict(W.WEIGHTS)

    def score(self, bundle: EnrichmentBundle) -> HolisticScore:
        dimensions = [
            self._safety(bundle),
            self._family(bundle),
            self._education(bundle),
            self._environment(bundle),
            self._structural(bundle),
        ]
        total = sum(d.weighted for d in dimensions)
        confidence = sum(d.weight * d.confidence for d in dimensions)

        return HolisticScore(
            property_id=bundle.property_id,
            total_score=round(clamp(total, 1.0, 10.0), 1),
            dimensions=dimensions,
            risk_flags=self._flags(bundle),
            confidence=round(confidence, 2),
            data_coverage_pct=bundle.coverage_pct,
        )

    # -- dimensions --------------------------------------------------------

    def _safety(self, bundle: EnrichmentBundle) -> DimensionScore:
        """Physical safety, from Politie registered-crime rates.

        Built from the four offence groups that bear on living somewhere with
        children, each scored against its own national rate, rather than from
        total registered crime — which in a nightlife district says more about
        the visitors than about the street.
        """
        result = bundle.crime
        if not result.usable or result.payload is None:
            return self._imputed(ScoreDimension.SAFETY, "no crime data")

        data = result.payload
        ratios = data.category_ratios or {}

        scored: list[tuple[str, float, float]] = []
        for group, weight in W.CRIME_GROUP_WEIGHTS.items():
            ratio = ratios.get(group)
            if ratio is None:
                continue
            scored.append(
                (group, weight, log_score(ratio, W.CRIME_RATIO_BEST, W.CRIME_RATIO_WORST))
            )

        drivers: list[str] = []
        if scored:
            total_weight = sum(w for _, w, _ in scored)
            score = sum(w * sc for _, w, sc in scored) / total_weight
            worst = min(scored, key=lambda item: item[2])
            drivers.append(
                f"{worst[0]} at {ratios[worst[0]]:.2f}x the national rate"
            )
            # Partial group coverage still scores, but says so.
            if total_weight < 0.999:
                drivers.append(
                    f"based on {len(scored)}/{len(W.CRIME_GROUP_WEIGHTS)} offence groups"
                )
        elif data.national_average_ratio is not None:
            # Fall back to all-crime when the groups came back empty.
            score = log_score(
                data.national_average_ratio, W.CRIME_RATIO_BEST, W.CRIME_RATIO_WORST
            )
            drivers.append(
                f"all registered crime at {data.national_average_ratio:.2f}x national"
            )
        else:
            return self._imputed(ScoreDimension.SAFETY, "crime data carried no usable rates")

        # A sustained trend nudges the score; it never dominates a level.
        if data.trend_12m_pct is not None:
            capped = max(-W.CRIME_TREND_PCT_FOR_MAX, min(W.CRIME_TREND_PCT_FOR_MAX, data.trend_12m_pct))
            score = clamp(score - W.CRIME_TREND_MAX_ADJUSTMENT * capped / W.CRIME_TREND_PCT_FOR_MAX)
            direction = "rising" if data.trend_12m_pct > 0 else "falling"
            drivers.append(f"crime {direction} {abs(data.trend_12m_pct):.0f}% year-on-year")

        level = data.area_level or "buurt"
        confidence = {"buurt": 1.0, "wijk": 0.7, "gemeente": 0.45}.get(level, 0.3)
        if level != "buurt":
            drivers.append(f"figures are {level}-level")

        return DimensionScore(
            dimension=ScoreDimension.SAFETY,
            score=round(score, 2),
            weight=self.weights[ScoreDimension.SAFETY],
            confidence=confidence,
            imputed=False,
            drivers=drivers,
        )

    def _family(self, bundle: EnrichmentBundle) -> DimensionScore:
        """Peer & family concentration, from CBS Kerncijfers wijken en buurten."""
        result = bundle.demographics
        if not result.usable or result.payload is None:
            return self._imputed(ScoreDimension.FAMILY, "no demographics data")

        data = result.payload
        if data.households_with_children_pct is None:
            return self._imputed(ScoreDimension.FAMILY, "households-with-children figure suppressed")

        pct = data.households_with_children_pct
        score = linear_score(pct, best=W.FAMILY_PCT_HIGH, worst=W.FAMILY_PCT_LOW)
        drivers = [
            f"{pct:.1f}% of households have children "
            f"(NL average {W.FAMILY_PCT_NATIONAL:.1f}%)"
        ]

        if data.age_0_15_pct is not None:
            drivers.append(f"{data.age_0_15_pct:.1f}% of residents are under 15")

        # Walking distance to a school or creche is a direct child-friendliness
        # signal, so it nudges the dimension rather than sitting inert.
        if data.distance_to_daycare_km is not None and data.distance_to_daycare_km <= 1.0:
            score = clamp(score + 0.3)
            drivers.append(f"creche {data.distance_to_daycare_km:.1f} km away")

        # A figure borrowed from the wijk or gemeente describes a wider area
        # than the house, so it is reported as lower-confidence, not as fact.
        level = data.area_level or "buurt"
        confidence = {"buurt": 1.0, "wijk": 0.7, "gemeente": 0.45}.get(level, 0.3)
        if level != "buurt":
            drivers.append(f"figures are {level}-level (buurt data suppressed by CBS)")

        return DimensionScore(
            dimension=ScoreDimension.FAMILY,
            score=round(score, 2),
            weight=self.weights[ScoreDimension.FAMILY],
            confidence=confidence,
            imputed=False,
            drivers=drivers,
        )

    def _education(self, bundle: EnrichmentBundle) -> DimensionScore:
        """Education access, from the cached DUO registry.

        Built on proximity and choice, which are current and discriminating.
        Inspection verdicts are a 2018 snapshot in which four schools in five
        are simply "Voldoende", so they apply a capped penalty rather than
        carrying a share of the score.
        """
        result = bundle.education
        if not result.usable or result.payload is None:
            return self._imputed(ScoreDimension.EDUCATION, "no education data")

        data = result.payload
        if data.nearest_primary_distance_m is None:
            return self._imputed(ScoreDimension.EDUCATION, "no school found nearby")

        distance = data.nearest_primary_distance_m
        distance_score = log_score(
            max(distance, 1),
            best=W.SCHOOL_DISTANCE_EXCELLENT_M,
            worst=W.SCHOOL_DISTANCE_POOR_M,
        )
        choice_score = linear_score(
            data.primary_schools_within_1km or 0,
            best=W.SCHOOL_CHOICE_BEST,
            worst=W.SCHOOL_CHOICE_WORST,
        )
        score = (
            W.EDUCATION_DISTANCE_SHARE * distance_score
            + W.EDUCATION_CHOICE_SHARE * choice_score
        )

        drivers = [
            f"nearest primary school {distance} m away "
            f"(national median 900 m)",
            f"{data.primary_schools_within_1km or 0} primary school(s) within 1 km",
        ]
        if len(data.denominations) > 1:
            drivers.append(f"{len(data.denominations)} denominations within walking distance")

        # Capped penalty: a prompt to look into it, not a verdict.
        if data.poorly_rated_nearby:
            penalty = min(
                W.EDUCATION_POOR_RATING_PENALTY,
                W.EDUCATION_POOR_RATING_PENALTY_EACH * len(data.poorly_rated_nearby),
            )
            score = clamp(score - penalty)
            drivers.append(
                f"{len(data.poorly_rated_nearby)} nearby school(s) rated below Voldoende "
                f"in the {data.ratings_as_of or 'latest'} inspection snapshot"
            )

        return DimensionScore(
            dimension=ScoreDimension.EDUCATION,
            score=round(score, 2),
            weight=self.weights[ScoreDimension.EDUCATION],
            confidence=1.0,
            imputed=False,
            drivers=drivers,
        )

    def _environment(self, bundle: EnrichmentBundle) -> DimensionScore:
        """Environmental health: RIVM noise and air quality, plus livability.

        Any one part alone yields a score; the weights are re-normalised over
        whichever parts resolved, so a missing layer costs confidence rather
        than dragging the dimension toward zero.
        """
        parts: list[tuple[float, float]] = []   # (weight, score)
        drivers: list[str] = []

        noise = bundle.noise.payload if bundle.noise.usable else None
        if noise is not None:
            level = noise.cumulative_lden_db
            if level is None and noise.sources_mapped == 0:
                # An OK result with nothing mapped is a genuinely quiet spot,
                # not missing data — the provider distinguishes the two.
                parts.append((W.ENVIRONMENT_NOISE_SHARE, 10.0))
                drivers.append("no mapped noise source at this address")
            elif level is not None:
                parts.append((
                    W.ENVIRONMENT_NOISE_SHARE,
                    linear_score(level, best=W.NOISE_LDEN_GOOD_DB, worst=W.NOISE_LDEN_BAD_DB),
                ))
                drivers.append(f"{level:.0f} dB Lden from all sources combined")

        air = bundle.air.payload if bundle.air.usable else None
        if air is not None:
            # Score each pollutant against its own WHO guideline and blend, so
            # the parts stay comparable and a single bad pollutant is visible
            # instead of averaged away in µg/m³ across different scales.
            scored: list[tuple[float, float]] = []
            for key, weight in W.AIR_POLLUTANT_WEIGHTS.items():
                value = getattr(air, f"{key}_ug_m3", None)
                if value is None:
                    continue
                ratio = value / W.WHO_ANNUAL_GUIDELINE_UG_M3[key]
                scored.append(
                    (weight, linear_score(ratio, best=W.AIR_RATIO_BEST, worst=W.AIR_RATIO_WORST))
                )
            if scored:
                weight_sum = sum(w for w, _ in scored)
                parts.append((
                    W.ENVIRONMENT_AIR_SHARE,
                    sum(w * sc for w, sc in scored) / weight_sum,
                ))
                readings = ", ".join(
                    f"{key.upper()} {getattr(air, f'{key}_ug_m3'):.0f}"
                    for key in W.AIR_POLLUTANT_WEIGHTS
                    if getattr(air, f"{key}_ug_m3", None) is not None
                )
                drivers.append(f"air quality {readings} µg/m³ (WHO 2021 annual guidelines)")

        livability = bundle.leefbaarometer.payload if bundle.leefbaarometer.usable else None
        if livability is not None:
            # Prefer the physical-environment sub-score over the composite.
            # The composite folds in a safety class that the safety dimension
            # already scores from Politie data, so using it here would count
            # crime twice; "fysieke omgeving" is the part this dimension is
            # actually about. The composite is the fallback, not the default.
            physical = livability.dimension_classes.get("fysieke_omgeving")
            ordinal = physical if physical is not None else livability.klasse_ordinal
            if ordinal is not None:
                parts.append((
                    W.ENVIRONMENT_LIVABILITY_SHARE,
                    linear_score(ordinal, best=9, worst=1),
                ))
                drivers.append(
                    f"Leefbaarometer physical environment class {physical}/9"
                    if physical is not None
                    else f"Leefbaarometer overall class '{livability.klasse or ordinal}'"
                )

        if not parts:
            return self._imputed(
                ScoreDimension.ENVIRONMENT, "no noise, air-quality or livability data")

        total_weight = sum(w for w, _ in parts)
        score = sum(w * sc for w, sc in parts) / total_weight

        return DimensionScore(
            dimension=ScoreDimension.ENVIRONMENT,
            score=round(score, 2),
            weight=self.weights[ScoreDimension.ENVIRONMENT],
            # Confidence tracks how much of the dimension actually resolved.
            confidence=round(total_weight, 2),
            imputed=False,
            drivers=drivers,
        )

    def _structural(self, bundle: EnrichmentBundle) -> DimensionScore:
        """Structural security: RVO's foundation-risk areas plus RIVM's
        flood-probability class.

        Either part alone yields a score; the weights are re-normalised over
        whichever parts resolved, so a missing flood sample costs confidence
        rather than dragging the dimension toward the imputed midpoint.
        Subsidence and flood depth still have no reachable national source, so
        the dimension does not pretend to include them.
        """
        result = bundle.soil
        if not result.usable or result.payload is None:
            return self._imputed(ScoreDimension.STRUCTURAL, "no soil/foundation data")

        data = result.payload
        parts: list[tuple[float, float]] = []
        drivers: list[str] = []
        year_known = data.construction_year_known

        if data.foundation_risk_ordinal is not None:
            parts.append((
                W.STRUCTURAL_FOUNDATION_SHARE,
                linear_score(data.foundation_risk_ordinal,
                            best=W.FOUNDATION_ORDINAL_BEST, worst=W.FOUNDATION_ORDINAL_WORST),
            ))
            driver = f"foundation risk '{data.foundation_risk_class}'"
            if data.soil_vulnerability:
                ground = data.soil_vulnerability
                if data.soil_type and data.soil_type.lower() != "niet indeelbaar":
                    ground += f", {data.soil_type}"
                driver += f" ({ground})"
            drivers.append(driver)
            if not year_known:
                drivers.append(
                    f"construction year unknown; inferred from the {data.area_pre_1970_pct:.0f}% "
                    f"of this postcode built pre-1970" if data.area_pre_1970_pct is not None
                    else "construction year unknown; inferred from the area"
                )

        if data.flood_risk_ordinal is not None:
            parts.append((
                W.STRUCTURAL_FLOOD_SHARE,
                linear_score(data.flood_risk_ordinal,
                            best=W.FLOOD_ORDINAL_BEST, worst=W.FLOOD_ORDINAL_WORST),
            ))
            drivers.append(f"flood probability '{data.flood_risk_class}'")

        if not parts:
            return self._imputed(ScoreDimension.STRUCTURAL, "no foundation classification")

        total_weight = sum(w for w, _ in parts)
        score = sum(w * sc for w, sc in parts) / total_weight

        # A missing flood sample and an inferred construction year are two
        # independent reasons to trust this number a little less; they stack.
        confidence = round(total_weight, 2)
        if data.foundation_risk_ordinal is not None and not year_known:
            confidence *= W.STRUCTURAL_CONFIDENCE_INFERRED_YEAR / W.STRUCTURAL_CONFIDENCE_KNOWN_YEAR

        return DimensionScore(
            dimension=ScoreDimension.STRUCTURAL,
            score=round(score, 2),
            weight=self.weights[ScoreDimension.STRUCTURAL],
            confidence=round(confidence, 2),
            imputed=False,
            drivers=drivers,
        )

    def _imputed(self, dimension: ScoreDimension, reason: str) -> DimensionScore:
        return DimensionScore(
            dimension=dimension,
            score=W.IMPUTED_SCORE,
            weight=self.weights[dimension],
            confidence=0.0,
            imputed=True,
            drivers=[f"imputed: {reason}"],
        )

    @staticmethod
    def _air_flags(air) -> list[RiskFlag]:
        """Flag each pollutant above its WHO 2021 annual guideline.

        Worth flagging even though almost every Dutch address exceeds at least
        one: the buyer is comparing addresses, and "1.6x" versus "2.4x" is the
        difference the flag makes visible. Severity is deliberately capped at
        `warn` — this is chronic background exposure, not a defect in the
        house, and nothing on this page is a reason to walk away on its own.
        """
        flags: list[RiskFlag] = []
        labels = {"no2": "NO2", "pm25": "PM2.5", "pm10": "PM10"}
        for key, label in labels.items():
            value = getattr(air, f"{key}_ug_m3", None)
            guideline = W.WHO_ANNUAL_GUIDELINE_UG_M3[key]
            if value is None or value <= guideline:
                continue
            flags.append(RiskFlag(
                code=f"AIR_{key.upper()}_ABOVE_WHO",
                severity="warn" if value >= guideline * 2 else "info",
                message=(
                    f"{label} {value:.0f} µg/m³ annual mean, "
                    f"{value / guideline:.1f}x the WHO 2021 guideline of {guideline:.0f}."
                ),
            ))
        return flags

    @staticmethod
    def _noise_flags(noise) -> list[RiskFlag]:
        """Flag each source that exceeds its own WHO guideline.

        Per-source rather than a single worst-case number, because the
        guidelines differ by source and the remedy does too — glazing helps
        against a motorway, nothing helps against a flight path.
        """
        flags: list[RiskFlag] = []
        lden = {
            "road": noise.road_lden_db,
            "rail": noise.rail_lden_db,
            "aviation": noise.aviation_lden_db,
        }
        for source, value in lden.items():
            guideline = W.WHO_LDEN_GUIDELINE_DB[source]
            if value is not None and value > guideline:
                excess = value - guideline
                flags.append(RiskFlag(
                    code=f"NOISE_{source.upper()}_ABOVE_WHO",
                    severity="critical" if excess >= 10 else "warn",
                    message=(
                        f"{source.capitalize()} noise {value:.0f} dB Lden, "
                        f"{excess:.0f} dB above the WHO guideline of {guideline:.0f}."
                    ),
                ))

        lnight = {"road": noise.road_lnight_db, "rail": noise.rail_lnight_db}
        for source, value in lnight.items():
            guideline = W.WHO_LNIGHT_GUIDELINE_DB[source]
            if value is not None and value > guideline:
                flags.append(RiskFlag(
                    code=f"NIGHT_NOISE_{source.upper()}_ABOVE_WHO",
                    severity="warn",
                    message=(
                        f"Night-time {source} noise {value:.0f} dB Lnight, above the "
                        f"WHO sleep guideline of {guideline:.0f} dB."
                    ),
                ))
        return flags

    @staticmethod
    def _soil_flags(soil) -> list[RiskFlag]:
        flags: list[RiskFlag] = []

        if soil.paalrot_risk:
            era = "pre-1970" if soil.construction_year_known else "probably pre-1970"
            flags.append(RiskFlag(
                code="PAALROT_RISK",
                severity="critical",
                message=(
                    f"Wooden-pile rot exposure: {era} construction on "
                    f"{soil.soil_vulnerability or 'vulnerable ground'}. "
                    "Commission a funderingsonderzoek before bidding."
                ),
            ))
        elif (soil.foundation_risk_ordinal or 0) >= 3:
            flags.append(RiskFlag(
                code="HIGH_FOUNDATION_RISK",
                severity="warn",
                message=f"Foundation risk rated '{soil.foundation_risk_class}' for this postcode.",
            ))

        # A flag as well as a scored input: "1x per 10 jaar" is worth a buyer's
        # attention on its own, not only as a fraction of a composite number.
        if soil.flood_risk_ordinal is not None and soil.flood_risk_ordinal >= 4:
            flags.append(RiskFlag(
                code="ELEVATED_FLOOD_PROBABILITY",
                severity="critical" if soil.flood_risk_ordinal >= 5 else "warn",
                message=(
                    f"Flood probability '{soil.flood_risk_class}' (RIVM), given current "
                    "flood defences. No depth data is available — check the local "
                    "risicokaart for what a flood here would actually mean."
                ),
            ))

        if (soil.subsidence_mm_per_year or 0) >= W.SUBSIDENCE_SEVERE_MM_YR:
            flags.append(RiskFlag(
                code="SEVERE_SUBSIDENCE",
                severity="warn",
                message=f"Ground subsiding {soil.subsidence_mm_per_year:.1f} mm/yr.",
            ))
        return flags

    def _flags(self, bundle: EnrichmentBundle) -> list[RiskFlag]:
        """Hard warnings that a buyer should see regardless of the composite."""
        flags: list[RiskFlag] = []

        if bundle.noise.usable and bundle.noise.payload:
            flags.extend(self._noise_flags(bundle.noise.payload))

        if bundle.air.usable and bundle.air.payload:
            flags.extend(self._air_flags(bundle.air.payload))

        if bundle.soil.usable and bundle.soil.payload:
            flags.extend(self._soil_flags(bundle.soil.payload))

        if bundle.education.usable and bundle.education.payload:
            education = bundle.education.payload
            if education.poorly_rated_nearby:
                named = ", ".join(education.poorly_rated_nearby[:3])
                flags.append(RiskFlag(
                    code="POORLY_RATED_SCHOOL_NEARBY",
                    severity="info",
                    message=(
                        f"Rated below Voldoende in the {education.ratings_as_of or 'last'} "
                        f"inspection snapshot: {named}. DUO publishes no newer verdicts, "
                        "so check the school's current inspection report."
                    ),
                ))

        if bundle.coverage_pct < 50:
            flags.append(RiskFlag(
                code="LOW_DATA_COVERAGE",
                severity="info",
                message=f"Only {bundle.coverage_pct:.0f}% of data layers resolved — score is indicative.",
            ))
        return flags
