"""Address autocomplete — offline, against a stub HTTP client.

The shape here was verified against the live PDOK service before writing this
(see the module docstring in pdok_locatieserver.py): /suggest answers with
just {type, weergavenaam, id, score}, and a house number with a letter gets
its own separate suggestion row rather than one row with sub-options.
"""
from __future__ import annotations

import pytest

from app.services.geo.pdok_locatieserver import GeocodeError, PDOKLocatieserver

SUGGEST_RESPONSE = {
    "response": {
        "docs": [
            {"type": "adres", "weergavenaam": "Cruquiuskade 289, 1018AM Amsterdam",
             "id": "adr-3712db423e6d338b2087449056f0e98d", "score": 11.58},
            {"type": "adres", "weergavenaam": "Cruquiuskade 289A, 1018AM Amsterdam",
             "id": "adr-0bd6050551756dc140d619dd5ba9ec9d", "score": 10.15},
        ]
    }
}

LOOKUP_PLAIN = {
    "response": {"docs": [{
        "id": "adr-3712db423e6d338b2087449056f0e98d",
        "weergavenaam": "Cruquiuskade 289, 1018AM Amsterdam",
        "postcode": "1018AM", "huisnummer": "289", "huis_nlt": "289",
        "huisletter": None, "huisnummertoevoeging": None,
    }]}
}

LOOKUP_WITH_LETTER = {
    "response": {"docs": [{
        "id": "adr-0bd6050551756dc140d619dd5ba9ec9d",
        "weergavenaam": "Cruquiuskade 289A, 1018AM Amsterdam",
        "postcode": "1018AM", "huisnummer": "289", "huis_nlt": "289A",
        "huisletter": "A", "huisnummertoevoeging": None,
    }]}
}


class FakeHttp:
    def __init__(self, by_path):
        self.by_path = by_path
        self.calls = []

    async def get_json(self, url, **kw):
        self.calls.append((url, kw.get("params")))
        for path, payload in self.by_path.items():
            if path in url:
                return payload
        return {"response": {"docs": []}}


def service(by_path) -> PDOKLocatieserver:
    return PDOKLocatieserver(FakeHttp(by_path))


# -- suggest -------------------------------------------------------------


async def test_suggestions_carry_an_id_and_a_label():
    svc = service({"/suggest": SUGGEST_RESPONSE})
    results = await svc.suggest("Cruquiuskade 28")

    assert results == [
        {"id": "adr-3712db423e6d338b2087449056f0e98d",
         "label": "Cruquiuskade 289, 1018AM Amsterdam"},
        {"id": "adr-0bd6050551756dc140d619dd5ba9ec9d",
         "label": "Cruquiuskade 289A, 1018AM Amsterdam"},
    ]


async def test_a_short_query_is_not_sent_upstream():
    """Two characters cannot narrow anything down; save the call."""
    http = FakeHttp({"/suggest": SUGGEST_RESPONSE})
    results = await PDOKLocatieserver(http).suggest("Cr")

    assert results == []
    assert http.calls == []


async def test_it_hits_suggest_not_free():
    """The whole reason this exists separately from resolve()/search(): a
    still-being-typed query needs the endpoint tuned for that, not the one
    tuned for a complete address."""
    http = FakeHttp({"/suggest": SUGGEST_RESPONSE})
    await PDOKLocatieserver(http).suggest("Cruquiuskade 28")

    assert http.calls[0][0].endswith("/suggest")


async def test_an_upstream_failure_degrades_to_an_empty_list():
    """A stalled dropdown is a UI inconvenience, not an error to surface
    mid-keystroke."""
    class Boom:
        async def get_json(self, *a, **kw):
            from app.core.http_client import TransientHTTPError
            raise TransientHTTPError("down")

    assert await PDOKLocatieserver(Boom()).suggest("Cruquiuskade 28") == []


async def test_docs_missing_an_id_or_label_are_dropped():
    http = FakeHttp({"/suggest": {"response": {"docs": [
        {"type": "adres", "weergavenaam": "No id here"},
        {"type": "adres", "id": "adr-x"},
    ]}}})
    assert await PDOKLocatieserver(http).suggest("something") == []


# -- address_by_id ---------------------------------------------------------


async def test_a_plain_address_resolves_with_no_addition():
    svc = service({"/lookup": LOOKUP_PLAIN})
    result = await svc.address_by_id("adr-3712db423e6d338b2087449056f0e98d")

    assert result == {
        "postal_code": "1018AM", "house_number": "289",
        "house_number_addition": None,
        "address": "Cruquiuskade 289, 1018AM Amsterdam",
    }


async def test_a_house_letter_becomes_the_addition():
    svc = service({"/lookup": LOOKUP_WITH_LETTER})
    result = await svc.address_by_id("adr-0bd6050551756dc140d619dd5ba9ec9d")

    assert result["house_number"] == "289"
    assert result["house_number_addition"] == "A"


async def test_an_unknown_id_is_a_clear_error():
    svc = service({})
    with pytest.raises(GeocodeError, match="no address found"):
        await svc.address_by_id("adr-does-not-exist")


async def test_a_doc_with_no_postcode_is_refused():
    svc = service({"/lookup": {"response": {"docs": [
        {"id": "adr-x", "huisnummer": "1", "huis_nlt": "1"}
    ]}}})
    with pytest.raises(GeocodeError, match="no usable postcode"):
        await svc.address_by_id("adr-x")
