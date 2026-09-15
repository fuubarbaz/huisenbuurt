"""Renovation estimator tests — offline, against a fixture cost table."""
from __future__ import annotations

import json

import pytest

from app.services.renovation import HOUSE_TYPES, RenovationEstimator

TABLE = {
    "source": "Verbeterjehuis.nl (Milieu Centraal)",
    "source_url": "https://www.verbeterjehuis.nl/",
    "measures": [
        {"measure": "gevelisolatie", "title": "Gevelisolatie", "url": "https://x/gevel",
         "by_house_type": {
             "terraced": {"cost_eur": 800, "subsidy_eur": 200, "saving_eur_year": 240},
             "detached": {"cost_eur": 2700, "subsidy_eur": 700, "saving_eur_year": 800}}},
        {"measure": "vloerisolatie", "title": "Vloerisolatie", "url": "https://x/vloer",
         "by_house_type": {
             "terraced": {"cost_eur": 2150, "subsidy_eur": 260, "saving_eur_year": 110},
             "detached": {"cost_eur": 4200, "subsidy_eur": 500, "saving_eur_year": 340}}},
        {"measure": "dakisolatie", "title": "Dakisolatie", "url": "https://x/dak",
         "by_house_type": {
             "terraced": {"cost_eur": 6000, "subsidy_eur": 850, "saving_eur_year": 460},
             "detached": {"cost_eur": 10000, "subsidy_eur": 1500, "saving_eur_year": 750}}},
        {"measure": "isolatieglas", "title": "Isolatieglas", "url": "https://x/glas",
         "by_house_type": {
             "terraced": {"cost_eur": 4400, "subsidy_eur": 500, "saving_eur_year": 350}}},
    ],
}


@pytest.fixture
def estimator(tmp_path):
    path = tmp_path / "renovation_costs.json"
    path.write_text(json.dumps(TABLE))
    return RenovationEstimator(path)


# -- from one label to another -----------------------------------------------


def test_the_work_between_two_labels_is_a_set_difference(estimator):
    """What a G house still needs, minus what a C house still needs."""
    est = estimator.between(current_label="G", target_label="C", house_type="terraced")

    assert {m.measure for m in est.measures} == {
        "gevelisolatie", "dakisolatie", "isolatieglas"}      # not vloerisolatie
    assert est.basis == "label target"
    assert est.energy_label == "G" and est.target_label == "C"


def test_a_smaller_step_costs_less(estimator):
    far = estimator.between(current_label="G", target_label="A", house_type="terraced")
    near = estimator.between(current_label="C", target_label="A", house_type="terraced")

    assert near.total_low_eur < far.total_low_eur
    assert len(near.measures) < len(far.measures)


def test_asking_for_where_you_already_are_costs_nothing(estimator):
    est = estimator.between(current_label="A", target_label="A")

    assert est.measures == []
    assert est.total_low_eur == 0


def test_asking_to_go_backwards_costs_nothing_rather_than_erroring(estimator):
    est = estimator.between(current_label="B", target_label="G")
    assert est.measures == []


def test_label_suffixes_are_read_as_their_letter(estimator):
    """A+++ is an A for this purpose."""
    plus = estimator.between(current_label="A+++", target_label="A")
    assert plus.measures == []


def test_an_unknown_label_is_treated_as_nothing_outstanding(estimator):
    """Better to claim no work than to invent some from a label we cannot read."""
    est = estimator.between(current_label="Z", target_label="A")
    assert est.measures == []


def test_the_house_type_still_collapses_the_range(estimator):
    ranged = estimator.between(current_label="G", target_label="A")
    exact = estimator.between(current_label="G", target_label="A", house_type="terraced")

    assert ranged.total_low_eur < ranged.total_high_eur
    assert exact.total_low_eur == exact.total_high_eur


def test_a_flat_is_still_flagged(estimator):
    est = estimator.between(current_label="G", target_label="A", units_in_building=9)
    assert est.is_apartment is True


def test_between_needs_the_table(tmp_path):
    assert RenovationEstimator(tmp_path / "absent.json").between(
        current_label="G", target_label="A") is None


# -- which measures apply ----------------------------------------------------


def test_a_pre_1976_house_needs_everything(estimator):
    est = estimator.estimate(construction_year=1890)

    assert est.basis == "construction year"
    assert {m.measure for m in est.measures} == {
        "gevelisolatie", "vloerisolatie", "dakisolatie", "isolatieglas"}


def test_an_eighties_house_needs_less(estimator):
    est = estimator.estimate(construction_year=1985)

    assert "gevelisolatie" not in {m.measure for m in est.measures}
    assert "dakisolatie" in {m.measure for m in est.measures}


def test_a_modern_build_needs_nothing(estimator):
    est = estimator.estimate(construction_year=2015)

    assert est.measures == []
    assert est.total_low_eur == 0
    assert est.payback_years is None


def test_the_energy_label_overrides_the_era(estimator):
    """A label reflects what is actually there; the year is only a proxy."""
    by_year = estimator.estimate(construction_year=1890)
    by_label = estimator.estimate(construction_year=1890, energy_label="C")

    assert by_label.basis == "energy label"
    assert len(by_label.measures) < len(by_year.measures)


def test_an_a_plus_label_is_read_as_modern(estimator):
    est = estimator.estimate(construction_year=1890, energy_label="A+++")

    assert est.basis == "energy label"
    assert est.measures == []


def test_no_year_and_no_label_yields_no_measures(estimator):
    est = estimator.estimate()
    assert est.basis == "none" and est.measures == []


# -- the cost range ----------------------------------------------------------


def test_an_unknown_house_type_gives_a_range(estimator):
    est = estimator.estimate(construction_year=1890)

    assert est.total_low_eur < est.total_high_eur
    assert est.house_type is None


def test_a_known_house_type_collapses_the_range(estimator):
    est = estimator.estimate(construction_year=1890, house_type="terraced")

    assert est.total_low_eur == est.total_high_eur
    assert est.total_low_eur == 800 + 2150 + 6000 + 4400


def test_an_unrecognised_house_type_falls_back_to_the_range(estimator):
    est = estimator.estimate(construction_year=1890, house_type="houseboat")

    assert est.house_type is None
    assert est.total_low_eur < est.total_high_eur


def test_totals_and_payback_are_computed(estimator):
    est = estimator.estimate(construction_year=1890, house_type="terraced")

    assert est.total_subsidy_eur == 200 + 260 + 850 + 500
    assert est.total_saving_eur_year == 240 + 110 + 460 + 350
    assert est.payback_years == pytest.approx(
        (est.total_low_eur - est.total_subsidy_eur) / est.total_saving_eur_year, abs=0.1)


def test_the_computed_totals_survive_serialisation(estimator):
    dumped = estimator.estimate(construction_year=1890, house_type="terraced").model_dump()

    for key in ("total_low_eur", "total_high_eur", "total_subsidy_eur",
                "total_saving_eur_year", "payback_years"):
        assert key in dumped


# -- flats -------------------------------------------------------------------


def test_a_shared_building_is_marked_as_a_flat(estimator):
    """The roof and facade belong to the VvE, not to the buyer."""
    est = estimator.estimate(construction_year=1890, units_in_building=9)
    assert est.is_apartment is True

    house = estimator.estimate(construction_year=1890, units_in_building=1)
    assert house.is_apartment is False


# -- provenance and absence --------------------------------------------------


def test_the_source_is_carried_through(estimator):
    est = estimator.estimate(construction_year=1890)

    assert "Milieu Centraal" in est.source
    assert est.source_url.startswith("https://www.verbeterjehuis.nl")
    assert all(m.url for m in est.measures)


def test_no_table_means_no_estimate_rather_than_a_guess(tmp_path):
    estimator = RenovationEstimator(tmp_path / "absent.json")

    assert estimator.available is False
    assert estimator.estimate(construction_year=1890) is None


def test_house_types_are_the_published_ones():
    assert HOUSE_TYPES == ("terraced", "end_terrace", "semi_detached", "detached")
