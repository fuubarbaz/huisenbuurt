"""REST facade tests.

The point of these is the seam, not the scoring: `app/main.py` should be pure
wiring over the same services the CLI uses, so the enricher and scorer are
swapped for fakes and only the HTTP behaviour is exercised.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.models.enrichment import EnrichmentBundle, ProviderResult, ProviderStatus
from app.models.property import GeoIdentity
from app.models.score import HolisticScore
from app.services.enrichment import GeocodeFailed

LISTING_BODY = {
    "property_id": "api-1",
    "source": "manual",
    "url": "https://example.invalid/1",
    "address": "Teststraat 1",
    "postal_code": "1015AA",
    "house_number": "1",
    "construction_year": 1975,
    "price_eur": 650_000,
    "living_area_m2": 120,
}


def bundle(pid="api-1") -> EnrichmentBundle:
    layers = {n: ProviderResult(provider=n, status=ProviderStatus.MISSING)
              for n in EnrichmentBundle.LAYER_NAMES}
    return EnrichmentBundle(
        property_id=pid, geo=GeoIdentity(latitude=52.3, longitude=4.9), **layers)


class FakeEnricher:
    def __init__(self, fail=False): self.fail = fail

    async def enrich_one(self, listing, geo=None, only=None):
        if self.fail:
            raise GeocodeFailed(f"{listing.property_id}: no such address")
        return bundle(listing.property_id)


class FakeScorer:
    def score(self, b) -> HolisticScore:
        return HolisticScore(property_id=b.property_id, total_score=6.9,
                             confidence=1.0, data_coverage_pct=100.0, dimensions=[])


@pytest.fixture
def client(monkeypatch):
    """Bypass lifespan so no HTTP client or database is opened."""
    monkeypatch.setitem(main_module.state, "enricher", FakeEnricher())
    monkeypatch.setitem(main_module.state, "scorer", FakeScorer())
    with TestClient(main_module.app) as c:
        # TestClient runs lifespan, which overwrites state with the real
        # objects; put the fakes back before any request is made.
        main_module.state["enricher"] = FakeEnricher()
        main_module.state["scorer"] = FakeScorer()
        yield c


# -- discoverability ---------------------------------------------------------


def test_root_is_a_map_not_a_404():
    """A bare 404 at the root of a freshly started service tells you nothing."""
    with TestClient(main_module.app) as c:
        r = c.get("/")

    assert r.status_code == 200
    body = r.json()
    assert body["docs"] == "/docs"
    assert "POST /score" in body["endpoints"]


def test_health_is_liveness_only():
    with TestClient(main_module.app) as c:
        assert c.get("/health").json() == {"status": "ok"}


def test_openapi_schema_is_served():
    with TestClient(main_module.app) as c:
        schema = c.get("/openapi.json").json()

    assert schema["info"]["title"] == "WoonAgent API"
    assert "/score" in schema["paths"]


# -- renovation --------------------------------------------------------------


def test_renovation_is_requested_separately_not_bundled_with_the_score(client):
    """The target label is the buyer's choice, so it cannot be precomputed."""
    body = client.post("/score", json=LISTING_BODY).json()
    assert "renovation" not in body or body.get("renovation") is None


def test_the_renovation_endpoint_needs_both_labels(client):
    assert client.post("/renovation", json={"current_label": "F"}).status_code == 422


# -- scoring -----------------------------------------------------------------


def test_score_returns_the_full_record(client):
    r = client.post("/score", json=LISTING_BODY)

    assert r.status_code == 200
    body = r.json()
    assert body["listing"]["postal_code"] == "1015AA"
    assert body["score"]["total_score"] == 6.9
    assert body["enrichment"]["property_id"] == "api-1"


def test_score_is_post_only(client):
    assert client.get("/score").status_code == 405


def test_an_invalid_postcode_is_a_422(client):
    r = client.post("/score", json={**LISTING_BODY, "postal_code": "not-a-postcode"})
    assert r.status_code == 422


def test_a_missing_required_field_is_a_422(client):
    body = {k: v for k, v in LISTING_BODY.items() if k != "postal_code"}
    assert client.post("/score", json=body).status_code == 422


def test_an_ungeocodable_address_is_a_422_not_a_500(client):
    main_module.state["enricher"] = FakeEnricher(fail=True)
    r = client.post("/score", json=LISTING_BODY)

    assert r.status_code == 422
    assert "no such address" in r.json()["detail"]
