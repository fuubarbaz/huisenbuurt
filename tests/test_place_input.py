"""Parsing the things people actually paste.

A destination or an address is rarely typed cleanly — it is copied out of
Google Maps, a listing, or an email. These pin the formats that must work.
"""
from __future__ import annotations

import pytest

from app.services.geo.pdok_locatieserver import (
    _covers_query,
    parse_coordinates,
    strip_place_url,
)


# -- coordinates -------------------------------------------------------------


def test_bare_coordinates_are_read():
    assert parse_coordinates("52.3791, 4.9003") == pytest.approx((52.3791, 4.9003))
    assert parse_coordinates("52.3791,4.9003") == pytest.approx((52.3791, 4.9003))


def test_coordinates_are_read_out_of_a_maps_url():
    url = "https://www.google.com/maps/place/Amsterdam+Centraal/@52.3791,4.9003,17z"
    assert parse_coordinates(url) == pytest.approx((52.3791, 4.9003))


def test_the_data_layer_form_is_also_read():
    """Maps also writes the point as !3d<lat>!4d<lon> further along the URL."""
    url = "https://www.google.com/maps/place/X/data=!3d52.3791!4d4.9003"
    assert parse_coordinates(url) == pytest.approx((52.3791, 4.9003))


def test_a_point_outside_the_netherlands_is_refused():
    """Every source behind this engine is Dutch; elsewhere is a mistake."""
    assert parse_coordinates("48.8584, 2.2945") is None       # Eiffel Tower
    assert parse_coordinates("40.7128, -74.0060") is None     # New York


def test_things_that_are_not_coordinates_are_left_alone():
    assert parse_coordinates("Kalverstraat 1 Amsterdam") is None
    assert parse_coordinates("1012AB 1") is None
    assert parse_coordinates("") is None


# -- maps URLs ---------------------------------------------------------------


def test_a_place_name_is_recovered_from_a_maps_url():
    """A URL searched verbatim matches nothing."""
    assert strip_place_url(
        "https://www.google.com/maps/place/Amsterdam+Centraal/") == "Amsterdam Centraal"


def test_url_escaping_is_undone():
    assert strip_place_url(
        "https://www.google.com/maps/place/Den+Haag+HS%2C+Den+Haag/"
    ) == "Den Haag HS, Den Haag"


def test_plain_text_passes_through_untouched():
    assert strip_place_url("Kalverstraat 1 Amsterdam") == "Kalverstraat 1 Amsterdam"


# -- copied addresses --------------------------------------------------------


def test_a_country_suffix_does_not_block_a_match():
    """Google appends the country; PDOK, being Dutch, never echoes it back."""
    assert _covers_query(
        "Bos en Vaartlaan 1, 1181 AA Amstelveen, Netherlands",
        "Bos en Vaartlaan 1, 1181AA Amstelveen")
    assert _covers_query("Dam 1, 1012 JS Amsterdam, Nederland", "Dam 1, 1012JS Amsterdam")


def test_a_wrong_city_is_still_caught_through_the_noise():
    """The country allowance must not become a hole for real mismatches."""
    assert not _covers_query(
        "Kalverstraat 1, Amsterdam, Netherlands", "Kalverstraat 1, 3011AA Rotterdam")
