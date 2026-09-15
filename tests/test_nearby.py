"""Nearby comparables — in-memory SQLite, no network.

What this panel is not is the point. Recent *sales* are Kadaster's and
licensed; CBS publishes sale prices no finer than the municipality; Funda is
refused by decision. So these are asking prices of listings this agent has
already seen, and the tests pin the distance maths rather than pretending
otherwise.
"""
from __future__ import annotations

import pytest

from app.core.geometry import bounding_box, haversine_m
from app.db.repository import PropertyRepository
from app.models.property import GeoIdentity, ListingSource, PropertyListing
from app.models.score import HolisticScore

# Amsterdam, Cruquiuskade. The offsets below are chosen so the expected
# distances are round numbers at this latitude.
ORIGIN = (52.3706, 4.9385)


def listing(pid: str, price: int | None = 500_000, area: int | None = 100):
    return PropertyListing(
        property_id=pid, source=ListingSource.MANUAL, url=f"https://e.invalid/{pid}",
        address=f"Teststraat {pid}", postal_code="1018AM", house_number="1",
        price_eur=price, living_area_m2=area,
    )


def geo(lat: float, lon: float) -> GeoIdentity:
    return GeoIdentity(latitude=lat, longitude=lon, buurtnaam="Het Funen")


@pytest.fixture
async def repo():
    r = PropertyRepository("sqlite+aiosqlite:///:memory:")
    await r.init_schema()
    yield r
    await r.close()


async def seed(repo, pid, lat, lon, price=500_000, area=100, score=None):
    await repo.upsert_listing(listing(pid, price, area))
    await repo.save_geo(pid, geo(lat, lon))
    if score is not None:
        await repo.save_score(HolisticScore(
            property_id=pid, total_score=score, confidence=1.0,
            data_coverage_pct=100.0, dimensions=[]))


# -- the geometry ------------------------------------------------------------


def test_the_box_contains_the_circle():
    """Every edge must be at least the radius from the centre.

    A box even slightly tight would silently drop real neighbours. An
    over-wide one costs nothing, because the caller filters on true distance
    afterwards.
    """
    lat, lon = ORIGIN
    lat_min, lat_max, lon_min, lon_max = bounding_box(lat, lon, 1000)

    assert haversine_m(lat, lon, lat_max, lon) >= 1000       # north edge
    assert haversine_m(lat, lon, lat_min, lon) >= 1000       # south edge
    assert haversine_m(lat, lon, lat, lon_max) >= 1000       # east edge
    assert haversine_m(lat, lon, lat, lon_min) >= 1000       # west edge


def test_the_box_widens_with_latitude():
    """Longitude degrees shrink toward the poles; a fixed span would clip."""
    _, _, lo_s, hi_s = bounding_box(52.0, 5.0, 1000)
    _, _, lo_n, hi_n = bounding_box(53.5, 5.0, 1000)
    assert (hi_n - lo_n) > (hi_s - lo_s)


# -- the query ---------------------------------------------------------------


async def test_results_come_back_nearest_first(repo):
    await seed(repo, "far", 52.3706, 4.9600)      # ~1.5 km east
    await seed(repo, "near", 52.3706, 4.9400)     # ~100 m east
    await seed(repo, "mid", 52.3706, 4.9500)      # ~800 m east

    rows = await repo.nearby(latitude=ORIGIN[0], longitude=ORIGIN[1], radius_m=5000)

    assert [r["property_id"] for r in rows] == ["near", "mid", "far"]
    assert rows[0]["distance_m"] < rows[1]["distance_m"] < rows[2]["distance_m"]


async def test_the_radius_is_a_circle_not_the_box(repo):
    """A point in the box corner but outside the circle must be dropped, or
    the results would be up to 41% further away than asked for."""
    # Diagonally offset so it sits inside the 1 km box but beyond 1 km away.
    await seed(repo, "corner", ORIGIN[0] + 0.0085, ORIGIN[1] + 0.0135)

    inside_box = await repo.nearby(
        latitude=ORIGIN[0], longitude=ORIGIN[1], radius_m=1000)
    wider = await repo.nearby(
        latitude=ORIGIN[0], longitude=ORIGIN[1], radius_m=3000)

    assert inside_box == []
    assert [r["property_id"] for r in wider] == ["corner"]
    assert wider[0]["distance_m"] > 1000


async def test_the_property_itself_is_excluded(repo):
    await seed(repo, "self", *ORIGIN)
    await seed(repo, "other", 52.3706, 4.9400)

    rows = await repo.nearby(latitude=ORIGIN[0], longitude=ORIGIN[1],
                             radius_m=5000, exclude_property_id="self")

    assert [r["property_id"] for r in rows] == ["other"]


async def test_the_limit_is_applied_after_sorting(repo):
    """Truncating before the sort would return an arbitrary three, not the
    three nearest."""
    for i in range(6):
        await seed(repo, f"p{i}", 52.3706, 4.9385 + 0.002 * (6 - i))

    rows = await repo.nearby(latitude=ORIGIN[0], longitude=ORIGIN[1],
                             radius_m=5000, limit=3)

    assert [r["property_id"] for r in rows] == ["p5", "p4", "p3"]


async def test_price_per_m2_is_derived(repo):
    await seed(repo, "a", 52.3706, 4.9400, price=600_000, area=120)
    rows = await repo.nearby(latitude=ORIGIN[0], longitude=ORIGIN[1], radius_m=5000)

    assert rows[0]["price_per_m2"] == 5000


async def test_a_listing_without_a_price_still_appears(repo):
    """Absent is not the same as zero — it is shown with a blank, not hidden."""
    await seed(repo, "nopr", 52.3706, 4.9400, price=None, area=None)
    rows = await repo.nearby(latitude=ORIGIN[0], longitude=ORIGIN[1], radius_m=5000)

    assert rows[0]["property_id"] == "nopr"
    assert rows[0]["price_per_m2"] is None


async def test_an_unscored_listing_still_appears(repo):
    """The join to scores is a LEFT join: a listing the agent has seen but not
    yet scored is still a comparable."""
    await seed(repo, "unscored", 52.3706, 4.9400)
    await seed(repo, "scored", 52.3706, 4.9450, score=7.2)

    rows = {r["property_id"]: r for r in
            await repo.nearby(latitude=ORIGIN[0], longitude=ORIGIN[1], radius_m=5000)}

    assert rows["unscored"]["total_score"] is None
    assert rows["scored"]["total_score"] == 7.2


async def test_a_listing_with_no_geocode_is_absent(repo):
    """The join is on geo_identity, so an ungeocoded listing has no distance
    and cannot be placed."""
    await repo.upsert_listing(listing("noGeo"))

    rows = await repo.nearby(latitude=ORIGIN[0], longitude=ORIGIN[1], radius_m=5000)
    assert rows == []


async def test_an_empty_store_is_an_empty_list_not_an_error(repo):
    assert await repo.nearby(latitude=ORIGIN[0], longitude=ORIGIN[1]) == []
