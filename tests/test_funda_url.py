"""Funda URL parsing tests.

Funda is never fetched — see app/scrapers/funda.py — but a listing URL the user
pastes already carries the address, and that is all the engine needs. These
tests pin the parse, and the refusal to guess when it cannot.
"""
from __future__ import annotations

import pytest

from app.scrapers.funda_url import FundaUrlUnparseable, parse_funda_url

DETAIL = "https://www.funda.nl/detail/koop/amsterdam/huis-singel-30/43829102/"


# -- the shapes Funda publishes ----------------------------------------------


def test_current_detail_url():
    a = parse_funda_url(DETAIL)
    assert (a.street, a.house_number, a.city) == ("Singel", "30", "Amsterdam")
    assert a.listing_id == "43829102"
    assert a.query == "Singel 30 Amsterdam"


def test_older_url_with_the_id_inside_the_slug():
    a = parse_funda_url("https://www.funda.nl/koop/amsterdam/huis-43829102-singel-30/")
    assert (a.street, a.house_number, a.city) == ("Singel", "30", "Amsterdam")


def test_house_number_addition_is_split_out():
    a = parse_funda_url(
        "https://www.funda.nl/detail/koop/utrecht/appartement-nachtegaalstraat-12-a/1234567/")
    assert (a.house_number, a.addition) == ("12", "a")
    assert a.query == "Nachtegaalstraat 12a Utrecht"


def test_multiword_street_keeps_its_tussenvoegsel_lowercase():
    a = parse_funda_url(
        "https://www.funda.nl/detail/koop/amstelveen/huis-bos-en-vaartlaan-1/8765432/")
    assert a.street == "Bos en Vaartlaan"


def test_rental_urls_parse_too():
    a = parse_funda_url(
        "https://www.funda.nl/detail/huur/rotterdam/appartement-westersingel-289-b/9988776/")
    assert (a.street, a.house_number, a.addition) == ("Westersingel", "289", "b")


def test_scheme_is_optional():
    assert parse_funda_url("www.funda.nl/detail/koop/purmerend/huis-kwadijkerpark-69/554433/") \
        .street == "Kwadijkerpark"


@pytest.mark.parametrize("dwelling", ["huis", "woonhuis", "appartement", "studio", "villa"])
def test_every_dwelling_type_prefix_is_stripped(dwelling):
    url = f"https://www.funda.nl/detail/koop/breda/{dwelling}-teststraat-7/1234567/"
    assert parse_funda_url(url).street == "Teststraat"


# -- refusing to guess -------------------------------------------------------


def test_a_search_page_is_not_a_listing():
    with pytest.raises(FundaUrlUnparseable, match="no address slug"):
        parse_funda_url("https://www.funda.nl/koop/amsterdam/")


def test_an_app_share_link_gets_its_own_actionable_error():
    """Funda's in-app share button hands out /detail/<id>?utm_source=..., a
    listing id and tracking parameters only. There is no address anywhere in
    a URL like this, so the error must say that plainly rather than reuse the
    generic 'no address slug found' message, which reads as if the parser
    just failed to find something that is actually there."""
    url = ("https://www.funda.nl/detail/44411866"
           "?utm_source=funda&utm_medium=app&utm_campaign=share-listing-modal")
    with pytest.raises(FundaUrlUnparseable, match="shared from Funda's app"):
        parse_funda_url(url)


def test_a_bare_id_with_no_query_string_gets_the_same_error():
    with pytest.raises(FundaUrlUnparseable, match="shared from Funda's app"):
        parse_funda_url("https://www.funda.nl/detail/44411866")


def test_another_site_is_rejected():
    with pytest.raises(FundaUrlUnparseable, match="not a funda.nl URL"):
        parse_funda_url("https://huispedia.nl/amsterdam/1015aa/singel/30")


def test_a_slug_without_a_number_is_rejected():
    with pytest.raises(FundaUrlUnparseable):
        parse_funda_url("https://www.funda.nl/detail/koop/amsterdam/huis-singel/")


def test_an_empty_path_is_rejected():
    with pytest.raises(FundaUrlUnparseable):
        parse_funda_url("https://www.funda.nl")


# -- the parse must not become a fetch ---------------------------------------


def test_parsing_is_pure_string_work():
    """No network call may hide in here: Funda declines automated access.

    If this ever starts issuing requests, the import below will need httpx and
    the test will fail — which is the point.
    """
    import inspect

    from app.scrapers import funda_url

    source = inspect.getsource(funda_url)
    for forbidden in ("httpx", "requests", "urlopen", "HttpClient", "await "):
        assert forbidden not in source, f"{forbidden!r} appeared in a URL parser"
