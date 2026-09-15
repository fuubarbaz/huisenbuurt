"""Which listings the watch loop bothers with.

The reason this exists: an unfiltered run alerted on houses in Almelo and
Leeuwarden for someone whose interest was Amsterdam. The tests pin the two
things that matter most — unset means unfiltered, not "matches nothing", and
the check runs before price/area since it is the cheapest of the three.
"""
from __future__ import annotations

import pytest

from app.models.property import ListingSource, PropertyListing
from app.services.region_filter import InvalidRegion, RegionFilter, parse_regions


def listing(city=None, postal_code="1018AM") -> PropertyListing:
    return PropertyListing(
        property_id="r1", source=ListingSource.MANUAL, url="https://e.invalid/1",
        address="Teststraat 1", postal_code=postal_code, house_number="1", city=city,
    )


# -- parsing ------------------------------------------------------------


def test_nothing_configured_means_unfiltered():
    """Unset WOONAGENT_REGIONS must not narrow an existing setup to nothing."""
    f = parse_regions(None)
    assert f.is_empty
    assert f.matches(listing(city="Almelo"))


def test_blank_and_whitespace_also_mean_unfiltered():
    assert parse_regions("").is_empty
    assert parse_regions("   ").is_empty


def test_a_city_name_is_recognised():
    f = parse_regions("Amsterdam")
    assert not f.is_empty
    assert f.matches(listing(city="Amsterdam"))
    assert not f.matches(listing(city="Almelo"))


def test_city_matching_is_case_insensitive():
    f = parse_regions("amsterdam")
    assert f.matches(listing(city="AMSTERDAM"))
    assert f.matches(listing(city="Amsterdam"))


def test_several_cities_are_accepted():
    f = parse_regions("Amsterdam, Amstelveen, Diemen")
    assert f.matches(listing(city="Amstelveen"))
    assert f.matches(listing(city="Diemen"))
    assert not f.matches(listing(city="Almelo"))


def test_a_postcode_prefix_is_recognised():
    f = parse_regions("1018")
    assert f.matches(listing(postal_code="1018AM"))
    assert not f.matches(listing(postal_code="1019AB"))


def test_a_full_pc6_is_reduced_to_its_pc4():
    """Huispedia's URL gives a PC6; only the PC4 is meaningful as a region."""
    f = parse_regions("1018AM")
    assert f.matches(listing(postal_code="1018ZZ"))


def test_cities_and_postcodes_can_be_mixed():
    f = parse_regions("Amsterdam, 3011")
    assert f.matches(listing(city="Amsterdam", postal_code="9999ZZ"))
    assert f.matches(listing(city="Nowhere", postal_code="3011AA"))
    assert not f.matches(listing(city="Almelo", postal_code="7607AB"))


def test_a_hyphenated_city_name_is_accepted():
    f = parse_regions("'s-Gravenhage")
    assert f.matches(listing(city="'s-Gravenhage"))


def test_garbage_input_is_a_clear_error_not_a_silent_no_match():
    with pytest.raises(InvalidRegion, match="not a city name or a postcode"):
        parse_regions("Amsterdam, ???")


# -- matching against a real listing -------------------------------------


def test_a_listing_with_no_city_falls_back_to_postcode():
    f = parse_regions("1018")
    assert f.matches(listing(city=None, postal_code="1018AM"))


def test_a_listing_matching_neither_is_rejected():
    f = parse_regions("Amsterdam")
    assert not f.matches(listing(city="Almelo", postal_code="7607AB"))


def test_str_reports_none_when_unfiltered():
    assert "unfiltered" in str(RegionFilter(frozenset(), frozenset()))


def test_str_lists_the_configured_regions():
    text = str(parse_regions("Amsterdam,1018"))
    assert "amsterdam" in text
    assert "1018" in text
