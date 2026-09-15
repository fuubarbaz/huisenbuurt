"""PDOK Locatieserver tests — offline, against stubbed responses.

Locatieserver is a fuzzy search that always returns its best guess. These
tests pin the guard that stops a bad guess being scored as a real house.
"""
from __future__ import annotations

import pytest

from app.services.geo.pdok_locatieserver import (
    AddressMismatch,
    GeocodeError,
    PDOKLocatieserver,
    _same_house_number,
)


def doc(**kw):
    base = {
        "id": "adr-1", "weergavenaam": "Singel 30-H, 1015AA Amsterdam",
        "postcode": "1015AA", "huisnummer": 30, "score": 11.29,
        "straatnaam": "Singel", "woonplaatsnaam": "Amsterdam",
        "centroide_ll": "POINT(4.8931461 52.37844915)",
        "centroide_rd": "POINT(121359 487957)",
        "buurtcode": "BU0363AC01", "wijkcode": "WK0363AC", "gemeentecode": "0363",
        "gemeentenaam": "Amsterdam", "provincienaam": "Noord-Holland",
    }
    base.update(kw)
    return base


class FakeHttp:
    def __init__(self, docs): self.docs = docs

    async def get_json(self, url, **kw):
        return {"response": {"docs": self.docs}}


def geocoder(docs):
    return PDOKLocatieserver(FakeHttp(docs))


# -- the fuzzy-match guard ---------------------------------------------------


async def test_exact_match_resolves():
    geo = await geocoder([doc()]).resolve(postal_code="1015AA", house_number="30")

    assert geo.latitude == pytest.approx(52.3784, abs=1e-3)
    assert geo.longitude == pytest.approx(4.8931, abs=1e-3)
    assert geo.exact_house_number is True


async def test_a_different_postcode_is_rejected():
    """'9999ZZ 1' really does resolve to a street in Maastricht."""
    wrong = doc(weergavenaam="1 juli-weg 1G-01, Maastricht", postcode="6222AA", score=3.04)
    with pytest.raises(AddressMismatch, match="Maastricht"):
        await geocoder([wrong]).resolve(postal_code="9999ZZ", house_number="1")


async def test_a_missing_postcode_is_rejected_too():
    """The real Maastricht match carries no postcode field at all."""
    wrong = {"weergavenaam": "1 juli-weg 1G-01, Maastricht", "huisnummer": 1, "score": 3.04}
    with pytest.raises(AddressMismatch, match="absent"):
        await geocoder([wrong]).resolve(postal_code="9999ZZ", house_number="1")


async def test_wrong_house_number_in_the_right_postcode_is_a_soft_miss():
    """Still the right PC6, so it is recorded rather than raised."""
    near = doc(weergavenaam="Singel 16A, 1015AA Amsterdam", huisnummer=16)
    geo = await geocoder([near]).resolve(postal_code="1015AA", house_number="9999")

    assert geo.exact_house_number is False
    assert geo.buurtcode == "BU0363AC01"


async def test_no_match_at_all_raises():
    with pytest.raises(GeocodeError, match="no address match"):
        await geocoder([]).resolve(postal_code="1015AA", house_number="30")


async def test_postcode_comparison_ignores_spacing_and_case():
    geo = await geocoder([doc(postcode="1015 aa")]).resolve(
        postal_code="1015AA", house_number="30")
    assert geo.exact_house_number is True


# -- free-text resolution (no postcode) --------------------------------------


class SequencedHttp:
    """Returns a scripted doc per call, so resolve_text's two hops can differ."""

    def __init__(self, *docs): self.docs, self.calls = list(docs), 0

    async def get_json(self, url, **kw):
        doc = self.docs[min(self.calls, len(self.docs) - 1)]
        self.calls += 1
        return {"response": {"docs": [doc] if doc else []}}


async def test_free_text_resolves_and_then_verifies_by_postcode():
    matched = doc(straatnaam="Singel", woonplaatsnaam="Amsterdam")
    geo = await PDOKLocatieserver(SequencedHttp(matched, matched)).resolve_text(
        street="Singel", house_number="30", city="Amsterdam")

    assert geo.buurtcode == "BU0363AC01"


async def test_a_wrong_street_is_rejected():
    """Relevance cannot police this: "huis-te-koop-mooi 1" scores 13.1 against
    a real street in Leiden, higher than a correct match for Singel 30."""
    wrong = doc(straatnaam="Mooi Japiksteeg", woonplaatsnaam="Leiden",
                weergavenaam="Mooi Japiksteeg 1, 2311RR Leiden", postcode="2311RR", score=13.1)
    with pytest.raises(AddressMismatch, match="Mooi Japiksteeg"):
        await PDOKLocatieserver(SequencedHttp(wrong)).resolve_text(
            street="Te Koop Mooi", house_number="1", city="Leiden")


async def test_a_wrong_city_is_rejected():
    wrong = doc(straatnaam="Kleine Singel", woonplaatsnaam="Noordwolde", postcode="8391HS")
    with pytest.raises(AddressMismatch, match="Noordwolde"):
        await PDOKLocatieserver(SequencedHttp(wrong)).resolve_text(
            street="Kleine Singel", house_number="30", city="Amsterdam")


async def test_place_names_compare_loosely():
    """'s-Hertogenbosch from PDOK must match "S Hertogenbosch" from a URL slug."""
    matched = doc(straatnaam="Tolbrugstraat", woonplaatsnaam="'s-Hertogenbosch",
                  postcode="5211RW", weergavenaam="Tolbrugstraat 3, 5211RW 's-Hertogenbosch")
    geo = await PDOKLocatieserver(SequencedHttp(matched, matched)).resolve_text(
        street="Tolbrugstraat", house_number="3", city="S Hertogenbosch")

    assert geo is not None


async def test_no_match_at_all_raises_for_free_text():
    with pytest.raises(GeocodeError, match="no address match"):
        await PDOKLocatieserver(SequencedHttp(None)).resolve_text(
            street="Nowhere", house_number="1", city="Nowhereville")


# -- area code normalisation -------------------------------------------------


async def test_gemeentecode_is_prefixed_for_cbs():
    geo = await geocoder([doc()]).resolve(postal_code="1015AA", house_number="30")

    # Locatieserver returns it bare; CBS keys on the prefixed form.
    assert geo.gemeentecode == "GM0363"
    assert geo.buurtcode == "BU0363AC01"
    assert geo.wijkcode == "WK0363AC"


async def test_rijksdriehoek_is_parsed_when_present():
    geo = await geocoder([doc()]).resolve(postal_code="1015AA", house_number="30")
    assert geo.rd_x == pytest.approx(121359) and geo.rd_y == pytest.approx(487957)


# -- helper ------------------------------------------------------------------


def test_house_number_comparison_tolerates_additions():
    assert _same_house_number(30, "30") is True
    assert _same_house_number(30, "30A") is True     # addition in the request
    assert _same_house_number(16, "30") is False
    assert _same_house_number(None, "30") is False
    assert _same_house_number(30, "") is False
