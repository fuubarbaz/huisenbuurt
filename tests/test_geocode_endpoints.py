"""GET /geocode/suggest and GET /geocode/address — the REST facade over the
PDOK autocomplete service tested directly in test_geocode_suggest.py.

The point of these is the wiring: that the endpoints exist, call through to
PDOKLocatieserver correctly, and that autocomplete gets its own generous
rate-limit tier rather than sharing the budget /score protects.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.core.config import settings
from app.core.inbound_rate_limit import InboundRateLimiter
from app.services.geo.pdok_locatieserver import GeocodeError


class FakeGeocoder:
    def __init__(self, http=None):
        pass

    async def suggest(self, query):
        return [{"id": "adr-1", "label": f"{query} suggestion"}]

    async def address_by_id(self, doc_id):
        if doc_id == "missing":
            raise GeocodeError("no address found for suggestion id 'missing'")
        return {"postal_code": "1018AM", "house_number": "289",
                "house_number_addition": None, "address": "Cruquiuskade 289, Amsterdam"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main_module, "PDOKLocatieserver", FakeGeocoder)
    with TestClient(main_module.app) as c:
        yield c


def test_suggest_returns_the_geocoders_results(client):
    response = client.get("/geocode/suggest", params={"q": "Cruquiuskade 28"})
    assert response.status_code == 200
    assert response.json() == [{"id": "adr-1", "label": "Cruquiuskade 28 suggestion"}]


def test_suggest_requires_a_query_parameter(client):
    assert client.get("/geocode/suggest").status_code == 422


def test_address_returns_the_form_fields(client):
    response = client.get("/geocode/address", params={"id": "adr-1"})
    assert response.status_code == 200
    assert response.json()["postal_code"] == "1018AM"


def test_an_unknown_suggestion_id_is_a_422_not_a_500(client):
    response = client.get("/geocode/address", params={"id": "missing"})
    assert response.status_code == 422
    assert "no address found" in response.json()["detail"]


# -- the generous rate-limit tier --------------------------------------------


@pytest.fixture
def gated_with_tight_main_limit(monkeypatch):
    monkeypatch.setattr(main_module, "PDOKLocatieserver", FakeGeocoder)
    monkeypatch.setattr(settings, "access_token", "tok")
    # The main limiter would choke after 2 requests; suggest must not share it.
    monkeypatch.setattr(main_module, "_rate_limiter",
                        InboundRateLimiter(rate_per_minute=60, burst=2))
    monkeypatch.setattr(main_module, "_suggest_rate_limiter",
                        InboundRateLimiter(rate_per_minute=120, burst=20))
    with TestClient(main_module.app) as c:
        yield c


def test_autocomplete_survives_far_more_requests_than_the_main_budget_allows(
    gated_with_tight_main_limit,
):
    """Live typing an address debounced every ~300ms can easily exceed ten
    requests a minute; the main /score-protecting budget would make that
    feel broken, so autocomplete is metered separately."""
    client = gated_with_tight_main_limit
    for _ in range(10):
        response = client.get("/geocode/suggest", params={"q": "Cruquiuskade"},
                              headers={"X-Access-Token": "tok"})
        assert response.status_code == 200


def test_a_normal_route_still_hits_the_tight_main_budget(gated_with_tight_main_limit):
    """Confirms the fixture's main limiter is actually tight — otherwise the
    test above would prove nothing. /health is exempt from rate limiting
    entirely, so the probe has to be a real gated route."""
    client = gated_with_tight_main_limit
    for _ in range(2):
        client.get("/", headers={"X-Access-Token": "tok"})
    response = client.get("/", headers={"X-Access-Token": "tok"})
    assert response.status_code == 429
