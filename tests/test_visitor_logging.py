"""Who is actually visiting this — and the two bugs that would have hidden it.

Both are about middleware order and header trust on a proxied deployment, and
neither shows up on a laptop: `request.client.host` is only ever wrong when
something like Fly's edge sits in front of the process.
"""
from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.core.config import settings

TOKEN = "vis-token"


@pytest.fixture
def captured():
    """Everything the "visitors" logger emitted during the test."""
    records: list[str] = []

    class _Handler(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    logger = logging.getLogger("visitors")
    logger.setLevel(logging.INFO)
    handler = _Handler()
    logger.addHandler(handler)
    yield records
    logger.removeHandler(handler)


@pytest.fixture
def gated(monkeypatch):
    monkeypatch.setattr(settings, "access_token", TOKEN)
    monkeypatch.setattr(settings, "rate_limit_per_minute", 6000.0)
    monkeypatch.setattr(settings, "rate_limit_burst", 1000)
    with TestClient(main_module.app) as client:
        yield client


@pytest.fixture
def open_service(monkeypatch):
    monkeypatch.setattr(settings, "access_token", None)
    with TestClient(main_module.app) as client:
        yield client


# -- the real bug: a rejected request must still be logged -------------------


def test_a_request_rejected_for_a_bad_token_is_still_logged(gated, captured):
    """The whole point of this feature. Middleware order in Starlette is the
    opposite of source order — a naive placement made a 401 invisible here,
    the exact traffic worth seeing."""
    response = gated.get("/", headers={"X-Access-Token": "wrong"})

    assert response.status_code == 401
    assert any(" 401 " in line for line in captured)


def test_a_rate_limited_request_is_also_logged(monkeypatch, captured):
    from app.core.inbound_rate_limit import InboundRateLimiter
    monkeypatch.setattr(settings, "access_token", TOKEN)
    monkeypatch.setattr(main_module, "_rate_limiter", InboundRateLimiter(rate_per_minute=60, burst=1))

    with TestClient(main_module.app) as client:
        client.get("/", headers={"X-Access-Token": TOKEN})
        captured.clear()
        response = client.get("/", headers={"X-Access-Token": TOKEN})

    assert response.status_code == 429
    assert any(" 429 " in line for line in captured)


def test_a_successful_request_is_logged_with_its_status(gated, captured):
    gated.get("/health")
    assert any("GET /health 200" in line for line in captured)


def test_the_log_line_carries_method_path_and_timing(gated, captured):
    gated.get("/health")
    line = captured[-1]
    assert line.startswith("GET /health 200")
    assert "ms" in line


# -- the other bug: the logged IP must be the real visitor, not Fly's edge ---


def test_the_fly_client_ip_header_is_preferred(gated, captured):
    """Without this, every visitor behind Fly's proxy logs as the same
    internal 172.16-19.0.0/12 address — indistinguishable from each other and
    from Fly's own health-check prober."""
    gated.get("/health", headers={"Fly-Client-IP": "203.0.113.5"})
    assert "ip=203.0.113.5" in captured[-1]


def test_x_forwarded_for_is_the_fallback(gated, captured):
    gated.get("/health", headers={"X-Forwarded-For": "203.0.113.9, 10.0.0.1"})
    # Only the first hop: later ones can be appended by any proxy in between.
    assert "ip=203.0.113.9" in captured[-1]


def test_fly_client_ip_wins_over_x_forwarded_for(gated, captured):
    gated.get("/health", headers={"Fly-Client-IP": "203.0.113.5",
                                  "X-Forwarded-For": "198.51.100.1"})
    assert "ip=203.0.113.5" in captured[-1]


def test_with_neither_header_the_socket_address_is_used(gated, captured):
    """The laptop case: no edge proxy, so the socket address is correct."""
    gated.get("/health")
    assert "ip=testclient" in captured[-1]


# -- the same bug, but in the rate limiter's bucket key ----------------------


def test_distinct_visitors_do_not_share_a_rate_limit_bucket(monkeypatch):
    """Before the fix, every anonymous caller behind Fly's proxy was keyed on
    the same internal address, so one caller's traffic could exhaust the
    budget for everyone else hitting the app from a different network."""
    from app.core.inbound_rate_limit import InboundRateLimiter
    monkeypatch.setattr(settings, "access_token", TOKEN)
    monkeypatch.setattr(main_module, "_rate_limiter", InboundRateLimiter(rate_per_minute=60, burst=1))

    with TestClient(main_module.app) as client:
        first = client.get("/", headers={"Fly-Client-IP": "203.0.113.1"})
        second = client.get("/", headers={"Fly-Client-IP": "203.0.113.2"})

    assert first.status_code == 401          # no token supplied; rejected
    assert second.status_code == 401         # a different visitor, not 429
