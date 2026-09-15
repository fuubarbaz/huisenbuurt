"""The shared-secret gate that makes a public deployment safe to leave up.

Unset, it must be invisible — every existing test in the suite runs without a
token and must keep passing. Set, it must close everything except liveness,
because one /score is a dozen requests against other people's free APIs.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.core.config import settings

TOKEN = "s3cret-token"


@pytest.fixture
def gated(monkeypatch):
    monkeypatch.setattr(settings, "access_token", TOKEN)
    with TestClient(main_module.app) as client:
        yield client


@pytest.fixture
def open_service(monkeypatch):
    monkeypatch.setattr(settings, "access_token", None)
    with TestClient(main_module.app) as client:
        yield client


# -- off by default ----------------------------------------------------------


def test_no_token_configured_means_no_gate(open_service):
    """The laptop case. Configuring nothing must change nothing."""
    assert open_service.get("/").status_code == 200


# -- on ----------------------------------------------------------------------


def test_a_gated_service_refuses_an_anonymous_request(gated):
    response = gated.get("/")

    assert response.status_code == 401
    assert response.json()["detail"] == "access token required"


def test_health_answers_without_a_token(gated):
    """The platform health check has no way to send one, and an app that fails
    its own liveness probe is indistinguishable from an app that is down."""
    assert gated.get("/health").status_code == 200


def test_the_header_is_accepted(gated):
    assert gated.get("/", headers={"X-Access-Token": TOKEN}).status_code == 200


def test_a_wrong_token_is_refused(gated):
    assert gated.get("/", headers={"X-Access-Token": "wrong"}).status_code == 401


def test_the_query_parameter_is_accepted_and_remembered(gated):
    """One ?key=... visit has to authorise the UI's own later fetches, which
    cannot add a header to a relative fetch the page makes on its own."""
    first = gated.get("/ui", params={"key": TOKEN})
    assert first.status_code == 200
    assert "woonagent_access" in first.cookies

    # The client now carries the cookie; a bare request must succeed.
    assert gated.get("/").status_code == 200


def test_the_cookie_is_not_readable_from_javascript(gated):
    """It is a bearer token for the whole API, so a page-injected script must
    not be able to read it back out."""
    response = gated.get("/ui", params={"key": TOKEN})
    assert "httponly" in response.headers["set-cookie"].lower()


def test_posting_a_score_is_gated_too(gated):
    """The expensive route is the one that actually matters here."""
    assert gated.post("/score", json={}).status_code == 401
