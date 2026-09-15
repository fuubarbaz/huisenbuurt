"""User-chosen dimension weights.

Two things are being protected here. The first is that a weighting a slider UI
can actually produce — partial, unnormalised — still yields a score on the
1-10 scale. The second is that re-weighing stays pure: it must never reach for
the network, because the whole point of a separate /rescore is that dragging a
slider costs nothing upstream.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.models.enrichment import (
    CrimeStats,
    EnrichmentBundle,
    ProviderResult,
    ProviderStatus,
)
from app.models.property import GeoIdentity
from app.models.score import ScoreDimension
from app.services.scoring import ScoringEngine
from app.services.scoring import weights as W

LAYERS = EnrichmentBundle.LAYER_NAMES


def bundle() -> EnrichmentBundle:
    """A bundle with nothing in it, so every dimension is imputed to 5.5.

    Deliberate: with all five dimensions equal, the composite is 5.5 under any
    weighting, which isolates the normalisation from the scoring.
    """
    layers = {name: ProviderResult(provider=name, status=ProviderStatus.MISSING)
              for name in LAYERS}
    return EnrichmentBundle(
        property_id="w-1", geo=GeoIdentity(latitude=52.3, longitude=4.9), **layers)


# -- normalisation -----------------------------------------------------------


def test_nothing_supplied_keeps_the_calibrated_defaults():
    assert W.normalise(None) == W.WEIGHTS
    assert W.normalise({}) == W.WEIGHTS


def test_an_unnormalised_set_is_scaled_to_sum_to_one():
    """A slider UI emits whatever the user dragged to, never a set summing to 1."""
    result = W.normalise({d.value: 1.0 for d in ScoreDimension})

    assert sum(result.values()) == pytest.approx(1.0)
    assert result[ScoreDimension.SAFETY] == pytest.approx(0.2)


def test_a_partial_set_leaves_the_others_at_their_default_share():
    """Caring about schools must not silently zero the four not mentioned."""
    result = W.normalise({"education_quality_access": 0.60})

    assert sum(result.values()) == pytest.approx(1.0)
    assert result[ScoreDimension.EDUCATION] > W.WEIGHTS[ScoreDimension.EDUCATION]
    for dimension in ScoreDimension:
        assert result[dimension] > 0


def test_a_dimension_can_be_switched_off():
    result = W.normalise({"structural_security": 0.0})

    assert result[ScoreDimension.STRUCTURAL] == 0.0
    assert sum(result.values()) == pytest.approx(1.0)


def test_all_zero_is_refused_rather_than_quietly_defaulted():
    """The one input where guessing the intent would return a confident number
    the caller never asked for."""
    with pytest.raises(W.InvalidWeights, match="non-zero"):
        W.normalise({d.value: 0.0 for d in ScoreDimension})


def test_a_negative_weight_is_refused():
    with pytest.raises(W.InvalidWeights, match="zero or positive"):
        W.normalise({"physical_safety": -1.0})


def test_an_unknown_dimension_is_refused_by_name():
    with pytest.raises(W.InvalidWeights, match="unknown dimension"):
        W.normalise({"nice_neighbours": 1.0})


# -- the engine --------------------------------------------------------------


def test_the_engine_defaults_to_the_calibrated_weighting():
    assert ScoringEngine().weights == W.WEIGHTS


def test_the_reported_weights_are_the_ones_asked_for():
    """The client draws the bars from these, so they must be the weighting
    actually applied and not the module default."""
    weighting = W.normalise({d.value: 1.0 for d in ScoreDimension})
    score = ScoringEngine(weighting).score(bundle())

    for dimension in score.dimensions:
        assert dimension.weight == pytest.approx(0.2)


def test_an_imputed_dimension_also_carries_the_chosen_weight():
    """_imputed was a staticmethod reading the module constant, so it was the
    one path that would have kept reporting the default."""
    weighting = W.normalise({"physical_safety": 0.9})
    score = ScoringEngine(weighting).score(bundle())
    safety = next(d for d in score.dimensions if d.dimension is ScoreDimension.SAFETY)

    assert safety.imputed is True
    assert safety.weight == pytest.approx(weighting[ScoreDimension.SAFETY])


def test_an_all_imputed_bundle_is_weighting_invariant():
    """The control. Every dimension is 5.5, so no weighting can move the
    composite — which is what makes the next test's movement attributable to
    the weighting rather than to noise in the dimension scores."""
    b = bundle()
    default = ScoringEngine().score(b).total_score
    skewed = ScoringEngine(W.normalise({"physical_safety": 100.0})).score(b).total_score

    assert default == skewed == pytest.approx(5.5, abs=0.05)


def test_leaning_on_a_bad_dimension_lowers_the_composite():
    """The actual claim. A buurt with heavy crime should score worse for a
    buyer who says safety is all that matters."""
    bad_crime = ProviderResult(
        provider="politie", status=ProviderStatus.OK,
        payload=CrimeStats(area_level="buurt", area_code="BU0363AK06",
                           category_ratios={"burglaries": 4.0, "vandalism": 4.0,
                                            "nuisance": 4.0, "violent": 4.0}),
    )
    layers = {name: ProviderResult(provider=name, status=ProviderStatus.MISSING)
              for name in LAYERS}
    layers["crime"] = bad_crime
    b = EnrichmentBundle(property_id="w-2",
                         geo=GeoIdentity(latitude=52.3, longitude=4.9), **layers)

    default = ScoringEngine().score(b).total_score
    safety_first = ScoringEngine(W.normalise({"physical_safety": 100.0})).score(b).total_score

    assert safety_first < default


# -- the endpoints -----------------------------------------------------------


@pytest.fixture
def client():
    with TestClient(main_module.app) as c:
        yield c


def test_the_defaults_are_published_for_the_ui(client):
    """So the front end does not hardcode a second copy that can drift."""
    published = client.get("/weights").json()

    assert published == {d.value: w for d, w in W.WEIGHTS.items()}
    assert sum(published.values()) == pytest.approx(1.0)


def test_rescore_returns_a_score_for_the_supplied_weighting(client):
    response = client.post("/rescore", json={
        "enrichment": bundle().model_dump(mode="json"),
        "weights": {d.value: 1.0 for d in ScoreDimension},
    })

    assert response.status_code == 200
    body = response.json()
    assert body["property_id"] == "w-1"
    for dimension in body["dimensions"]:
        assert dimension["weight"] == pytest.approx(0.2)


def test_rescore_makes_no_upstream_calls(client, monkeypatch):
    """The reason this endpoint exists. If it geocoded, one slider drag would
    cost a dozen requests to other people's APIs."""
    def explode(*args, **kwargs):                      # pragma: no cover
        raise AssertionError("rescore must not touch the network")

    monkeypatch.setattr(main_module.PDOKLocatieserver, "resolve", explode)
    monkeypatch.setattr(main_module.EnrichmentPipeline, "enrich_one", explode)

    response = client.post("/rescore", json={
        "enrichment": bundle().model_dump(mode="json"), "weights": {}})
    assert response.status_code == 200


def test_rescore_rejects_an_all_zero_weighting_as_422(client):
    response = client.post("/rescore", json={
        "enrichment": bundle().model_dump(mode="json"),
        "weights": {d.value: 0.0 for d in ScoreDimension},
    })

    assert response.status_code == 422
    assert "non-zero" in response.json()["detail"]


def test_rescore_rejects_an_unknown_dimension_as_422(client):
    response = client.post("/rescore", json={
        "enrichment": bundle().model_dump(mode="json"),
        "weights": {"nice_neighbours": 1.0},
    })

    assert response.status_code == 422
