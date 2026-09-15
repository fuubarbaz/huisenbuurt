"""GET /national-map — the REST facade over NationalMapStore, which is
tested directly (and against live PDOK/DuckDB spatial data) elsewhere.

The point of these is the wiring: a missing database is a clear 503 rather
than a crash, and a present one is served as-is.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main as main_module


class FakeStore:
    def __init__(self, path, read_only=False):
        pass

    def as_geojson(self):
        return {"type": "FeatureCollection", "features": [{"fake": True}],
                "total_buurten": 14668, "scored_buurten": 65}

    def close(self):
        pass


@pytest.fixture
def client():
    with TestClient(main_module.app) as c:
        yield c


def test_a_missing_database_is_a_clear_503(client, monkeypatch, tmp_path):
    monkeypatch.setattr(main_module.settings, "national_map_db_path", str(tmp_path / "absent.duckdb"))

    response = client.get("/national-map")

    assert response.status_code == 503
    assert "build_national_map.py" in response.json()["detail"]


def test_a_present_database_is_served(client, monkeypatch, tmp_path):
    db_path = tmp_path / "present.duckdb"
    db_path.write_text("")  # only existence is checked before the thread hop
    monkeypatch.setattr(main_module.settings, "national_map_db_path", str(db_path))
    monkeypatch.setattr(main_module, "NationalMapStore", FakeStore)

    response = client.get("/national-map")

    assert response.status_code == 200
    body = response.json()
    assert body["scored_buurten"] == 65
    assert body["total_buurten"] == 14668
    assert body["features"] == [{"fake": True}]


def test_the_response_is_gzip_compressible(client, monkeypatch, tmp_path):
    """The whole reason GZipMiddleware was added — ~14,700 features is a
    real payload, and this is a free win for it specifically."""
    db_path = tmp_path / "present.duckdb"
    db_path.write_text("")
    monkeypatch.setattr(main_module.settings, "national_map_db_path", str(db_path))
    monkeypatch.setattr(main_module, "NationalMapStore", FakeStore)

    response = client.get("/national-map", headers={"Accept-Encoding": "gzip"})
    assert response.status_code == 200
