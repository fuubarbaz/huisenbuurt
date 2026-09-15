"""National map storage — a temp DuckDB file, no network, no spatial
extension needed (the store's own schema is plain columns; only the build
script's geometry pass touches DuckDB spatial — see its module docstring)."""
from __future__ import annotations

import pytest

from app.db.national_map_store import NationalMapStore


@pytest.fixture
def store(tmp_path):
    s = NationalMapStore(tmp_path / "test_map.duckdb")
    yield s
    s.close()


def seed_geometry(store, buurtcode="BU0363AK06", **overrides):
    defaults = dict(
        buurtcode=buurtcode, buurtnaam="Het Funen", gemeentecode="GM0363",
        gemeentenaam="Amsterdam", rd_x=123894.9, rd_y=486782.3,
        lat=52.368, lon=4.930,
        geometry_geojson='{"type":"Polygon","coordinates":[[[4.92,52.36],[4.94,52.36],[4.94,52.38],[4.92,52.36]]]}',
    )
    defaults.update(overrides)
    store.upsert_geometry(**defaults)


# -- geometry pass ------------------------------------------------------


def test_a_fresh_buurt_is_pending():
    pass  # covered by test_pending_lists_buurten_with_no_score below


def test_upserting_geometry_twice_does_not_duplicate(store):
    seed_geometry(store)
    seed_geometry(store, buurtnaam="Het Funen (renamed)")

    assert store.counts()["total"] == 1


def test_re_upserting_geometry_does_not_clear_an_existing_score(store):
    """A yearly boundary refresh must not wipe out an expensive score."""
    seed_geometry(store)
    store.save_score("BU0363AK06", safety=7.0, family=6.0, education=8.0,
                     environment=5.0, structural=7.0, confidence=1.0, coverage_pct=100.0)

    seed_geometry(store, buurtnaam="Het Funen (2025 boundary)")

    geojson = store.as_geojson()
    props = geojson["features"][0]["properties"]
    assert props["buurtnaam"] == "Het Funen (2025 boundary)"
    assert props["safety"] == 7.0          # untouched by the geometry re-upsert


# -- pending / resumability ----------------------------------------------


def test_pending_lists_buurten_with_no_score(store):
    seed_geometry(store, buurtcode="BU0001")
    seed_geometry(store, buurtcode="BU0002")

    pending = store.pending_buurten()
    assert {b["buurtcode"] for b in pending} == {"BU0001", "BU0002"}


def test_a_scored_buurt_drops_out_of_pending(store):
    seed_geometry(store, buurtcode="BU0001")
    seed_geometry(store, buurtcode="BU0002")
    store.save_score("BU0001", safety=5, family=5, education=5,
                     environment=5, structural=5, confidence=1.0, coverage_pct=100.0)

    pending = store.pending_buurten()
    assert [b["buurtcode"] for b in pending] == ["BU0002"]


def test_pending_carries_what_scoring_needs(store):
    seed_geometry(store, buurtcode="BU0001")
    pending = store.pending_buurten()[0]

    assert pending["rd_x"] == pytest.approx(123894.9)
    assert pending["lat"] == pytest.approx(52.368)
    assert pending["buurtnaam"] == "Het Funen"


def test_counts_report_total_and_scored_separately(store):
    seed_geometry(store, buurtcode="BU0001")
    seed_geometry(store, buurtcode="BU0002")
    store.save_score("BU0001", safety=5, family=5, education=5,
                     environment=5, structural=5, confidence=1.0, coverage_pct=100.0)

    assert store.counts() == {"total": 2, "scored": 1}


# -- serving --------------------------------------------------------------


def test_an_unscored_buurt_is_left_out_of_the_geojson(store):
    """Absent, not a false zero — a buurt with a shape but no score yet
    (a build in progress) must not render as if it scored zero."""
    seed_geometry(store, buurtcode="BU0001")

    assert store.as_geojson()["features"] == []


def test_a_scored_buurt_carries_its_geometry_and_all_five_dimensions(store):
    seed_geometry(store, buurtcode="BU0001")
    store.save_score("BU0001", safety=7.1, family=6.2, education=8.3,
                     environment=5.4, structural=7.5, confidence=0.9, coverage_pct=80.0)

    feature = store.as_geojson()["features"][0]
    assert feature["type"] == "Feature"
    assert feature["geometry"]["type"] == "Polygon"
    props = feature["properties"]
    assert (props["safety"], props["family"], props["education"],
            props["environment"], props["structural"]) == (7.1, 6.2, 8.3, 5.4, 7.5)
    assert props["confidence"] == 0.9
    assert props["coverage_pct"] == 80.0


def test_a_dimension_that_came_back_imputed_is_null_not_a_guess(store):
    """The scoring engine's IMPUTED_SCORE (5.5) must never leak in here as if
    it were a measured value — a missing dimension is stored as NULL."""
    seed_geometry(store, buurtcode="BU0001")
    store.save_score("BU0001", safety=7.0, family=None, education=8.0,
                     environment=5.0, structural=7.0, confidence=0.8, coverage_pct=80.0)

    props = store.as_geojson()["features"][0]["properties"]
    assert props["family"] is None


def test_reading_a_missing_database_raises_a_clear_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        NationalMapStore(tmp_path / "does_not_exist.duckdb", read_only=True)
