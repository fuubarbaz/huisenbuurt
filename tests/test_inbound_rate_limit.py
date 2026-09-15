"""The request budget that bounds what even a valid token can cost.

The token stops a stranger from using the API. This stops a leaked token, or
a script gone wrong, from turning a scale-to-zero Fly machine into a machine
that never goes idle — which is a dollar figure, not just bad manners.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.core.config import settings
from app.core.inbound_rate_limit import InboundRateLimiter

TOKEN = "budget-token"


# -- the bucket itself ---------------------------------------------------


def test_a_fresh_key_gets_its_full_burst():
    limiter = InboundRateLimiter(rate_per_minute=60, burst=3)
    for _ in range(3):
        allowed, _ = limiter.allow("k")
        assert allowed is True


def test_the_next_request_past_the_burst_is_refused():
    limiter = InboundRateLimiter(rate_per_minute=60, burst=2)
    limiter.allow("k")
    limiter.allow("k")
    allowed, retry_after = limiter.allow("k")

    assert allowed is False
    assert retry_after > 0


def test_different_keys_have_independent_budgets():
    """One caller's traffic must not exhaust another's — the whole point of
    keying on the token rather than a single global counter."""
    limiter = InboundRateLimiter(rate_per_minute=60, burst=1)
    limiter.allow("a")
    allowed, _ = limiter.allow("b")
    assert allowed is True


def test_tokens_refill_over_time(monkeypatch):
    limiter = InboundRateLimiter(rate_per_minute=60, burst=1)   # 1/sec
    limiter.allow("k")
    assert limiter.allow("k")[0] is False

    import app.core.inbound_rate_limit as mod
    now = mod.time.monotonic()
    monkeypatch.setattr(mod.time, "monotonic", lambda: now + 1.1)
    assert limiter.allow("k")[0] is True


def test_the_key_space_is_bounded():
    """An attacker cycling through fake keys must not grow memory forever."""
    limiter = InboundRateLimiter(rate_per_minute=60, burst=1)
    limiter._max_keys = 5
    for i in range(50):
        limiter.allow(f"key-{i}")
    assert len(limiter._buckets) <= 5


# -- wired into the app: off by default -----------------------------------


@pytest.fixture
def open_service(monkeypatch):
    monkeypatch.setattr(settings, "access_token", None)
    with TestClient(main_module.app) as client:
        yield client


def test_no_token_configured_means_no_rate_limit_either(open_service):
    """Nothing on a laptop should change, including under the test suite's
    own rapid-fire requests — this is what stops 400 other tests from
    starting to see 429s the moment this feature landed."""
    for _ in range(50):
        assert open_service.get("/health").status_code == 200


# -- wired into the app: on when gated --------------------------------------


@pytest.fixture
def gated(monkeypatch):
    monkeypatch.setattr(settings, "access_token", TOKEN)
    monkeypatch.setattr(settings, "rate_limit_per_minute", 60.0)
    monkeypatch.setattr(settings, "rate_limit_burst", 3)
    # A fresh limiter per test: the real one is a module-level singleton and
    # would otherwise carry state between tests that share the same key.
    monkeypatch.setattr(main_module, "_rate_limiter",
                        InboundRateLimiter(rate_per_minute=60.0, burst=3))
    with TestClient(main_module.app) as client:
        yield client


def test_requests_within_the_burst_all_succeed(gated):
    for _ in range(3):
        assert gated.get("/", headers={"X-Access-Token": TOKEN}).status_code == 200


def test_the_request_past_the_burst_is_a_429(gated):
    for _ in range(3):
        gated.get("/", headers={"X-Access-Token": TOKEN})
    response = gated.get("/", headers={"X-Access-Token": TOKEN})

    assert response.status_code == 429
    assert response.json()["detail"] == "too many requests"
    assert "Retry-After" in response.headers


def test_health_is_exempt_from_the_budget_too(gated):
    """Same reasoning as the token exemption: the platform health check must
    always answer, or a busy period looks like a dead app."""
    for _ in range(10):
        assert gated.get("/health").status_code == 200


def test_a_wrong_token_still_counts_against_the_ip_budget(gated):
    """Someone guessing tokens must not get unlimited free guesses."""
    for _ in range(3):
        gated.get("/", headers={"X-Access-Token": "wrong"})
    response = gated.get("/", headers={"X-Access-Token": "wrong"})

    assert response.status_code == 429


def test_the_key_is_the_token_not_the_shared_ip(gated):
    """Two different tokens must not share a budget just because a request
    library reuses TestClient's fixed host — the token is the real identity
    once one is configured."""
    for _ in range(3):
        gated.get("/", headers={"X-Access-Token": TOKEN})
    # A different (even if also wrong) token gets its own bucket.
    response = gated.get("/", headers={"X-Access-Token": "someone-elses-token"})
    assert response.status_code == 401     # rejected for being wrong, not 429
