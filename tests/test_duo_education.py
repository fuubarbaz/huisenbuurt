"""DUO education provider and school index tests — offline, on a temp index."""
from __future__ import annotations

import sqlite3

import pytest

from app.models.enrichment import ProviderStatus
from app.models.property import GeoIdentity, ListingSource, PropertyListing
from app.services.enrichment.providers.base import EnrichmentContext
from app.services.enrichment.providers.duo_education import (
    NON_VERDICTS,
    POOR_RATINGS,
    RATING_ORDER,
    WALKABLE_RADIUS_M,
    DUOEducationProvider,
)
from app.services.enrichment.school_index import (
    SCHEMA,
    SchoolIndex,
    SchoolIndexMissing,
    haversine_m,
)

# Amsterdam Singel, the reference point for every fixture below.
LAT, LON = 52.3784, 4.8931


def offset(metres_north: float, metres_east: float = 0.0) -> tuple[float, float]:
    return (LAT + metres_north / 111_320.0,
            LON + metres_east / (111_320.0 * 0.61))


@pytest.fixture
def index_path(tmp_path):
    path = tmp_path / "schools.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    rows = [
        ("00AA00", "00AA", "Dichtbijschool", 300.0, "Openbaar", "Voldoende"),
        ("00BB00", "00BB", "Buurschool", 700.0, "Protestants-Christelijk", "Goed"),
        ("00CC00", "00CC", "Verderop", 1500.0, "Rooms-Katholiek", "Onvoldoende"),
        ("00DD00", "00DD", "Zonder oordeel", 900.0, "Openbaar", "Zonder actueel oordeel"),
        ("00EE00", "00EE", "Ver weg", 9000.0, "Openbaar", "Voldoende"),
    ]
    for code, brin, name, dist, denom, rating in rows:
        lat, lon = offset(dist)
        conn.execute(
            """INSERT INTO schools (vestigingscode, brin, name, education_type,
               postcode, city, denomination, latitude, longitude, rating, rating_as_of)
               VALUES (?,?,?,'po','1015AA','Amsterdam',?,?,?,?,'2018-09-01')""",
            (code, brin, name, denom, lat, lon, rating),
        )
    conn.executemany("INSERT INTO index_meta VALUES (?,?)",
                     [("schools", "5"), ("ratings_as_of", "2018-09-01")])
    conn.commit()
    conn.close()
    return path


def ctx() -> EnrichmentContext:
    listing = PropertyListing(
        property_id="e1", source=ListingSource.MANUAL, url="https://e.invalid",
        address="Teststraat 1", postal_code="1015AA", house_number="1",
    )
    return EnrichmentContext(listing=listing, geo=GeoIdentity(latitude=LAT, longitude=LON))


def build(path) -> DUOEducationProvider:
    p = DUOEducationProvider.__new__(DUOEducationProvider)
    p.http = None
    p.index = SchoolIndex(path)
    return p


# -- the index ---------------------------------------------------------------


def test_haversine_matches_known_distances():
    # One degree of latitude is close to 111 km.
    assert haversine_m(52.0, 5.0, 53.0, 5.0) == pytest.approx(111_195, rel=0.01)
    assert haversine_m(52.0, 5.0, 52.0, 5.0) == 0.0


def test_radius_query_is_ordered_and_bounded(index_path):
    found = SchoolIndex(index_path).nearby(LAT, LON, radius_m=2000)

    assert [s.name for s in found] == ["Dichtbijschool", "Buurschool", "Zonder oordeel", "Verderop"]
    assert all(s.distance_m <= 2000 for s in found)
    # The bounding box is a superset of the circle; the far school is excluded.
    assert "Ver weg" not in [s.name for s in found]


def test_missing_index_raises_a_named_error(tmp_path):
    with pytest.raises(SchoolIndexMissing, match="build_school_index"):
        SchoolIndex(tmp_path / "absent.db").nearby(LAT, LON)


# -- the provider ------------------------------------------------------------


async def test_nearest_and_walkable_counts(index_path):
    data = await build(index_path).fetch(ctx())

    assert data.nearest_primary_distance_m == pytest.approx(300, abs=5)
    # Within 1 km: 300 m, 700 m, 900 m — not the 1500 m one.
    assert data.primary_schools_within_1km == 3
    assert data.search_radius_m == 3000


async def test_non_verdicts_are_unrated_not_bad(index_path):
    data = await build(index_path).fetch(ctx())

    # Four schools in range, one carries "Zonder actueel oordeel".
    assert data.schools_rated == 3
    assert "Zonder oordeel" not in data.poorly_rated_nearby
    unrated = next(s for s in data.schools if s.name == "Zonder oordeel")
    assert unrated.inspection_rating is None


async def test_poor_ratings_are_named(index_path):
    data = await build(index_path).fetch(ctx())

    assert data.poorly_rated_nearby == ["Verderop"]
    # Two of the three rated schools are Voldoende or better.
    assert data.pct_rated_good_or_better == pytest.approx(66.7, abs=0.1)


async def test_rating_snapshot_date_travels_with_the_data(index_path):
    data = await build(index_path).fetch(ctx())
    assert data.ratings_as_of == "2018-09-01"


async def test_denominations_are_deduplicated(index_path):
    data = await build(index_path).fetch(ctx())
    assert data.denominations == ["Openbaar", "Protestants-Christelijk"]


async def test_missing_index_is_missing_with_instructions(tmp_path):
    result = await build(tmp_path / "absent.db").run(ctx(), timeout=5)

    assert result.status is ProviderStatus.MISSING
    assert "build_school_index" in result.error


async def test_no_school_in_range_is_missing(index_path):
    far = EnrichmentContext(
        listing=ctx().listing, geo=GeoIdentity(latitude=53.5, longitude=6.5))
    result = await build(index_path).run(far, timeout=5)

    assert result.status is ProviderStatus.MISSING
    assert "no primary school within" in result.error


# -- rating vocabulary -------------------------------------------------------


def test_rating_order_runs_worst_to_best():
    assert RATING_ORDER[0] == "zeer zwak"
    assert RATING_ORDER[-1] == "goed"
    assert set(POOR_RATINGS) < set(RATING_ORDER)


def test_absence_of_a_verdict_is_not_a_poor_verdict():
    assert not (NON_VERDICTS & POOR_RATINGS)
    assert WALKABLE_RADIUS_M == 1000
