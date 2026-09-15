"""Distance maths, shared by anything that needs "how far apart are these".

In ``app/core`` rather than beside its first caller because it now has two:
the school index, which measures schools against a property, and the
repository, which measures properties against each other. Importing the
enrichment service from the persistence layer to reach one trigonometric
function would close a dependency loop for no reason — the same mistake
``WozTrend`` and ``BuildingFacts`` were moved to ``app/models`` to avoid.
"""
from __future__ import annotations

import math

#: Mean Earth radius. Kept at the value the school index has always used, so
#: moving this function here does not shift a single published distance.
EARTH_RADIUS_M = 6_371_000.0

#: Metres in one degree, taken at its SHORTEST — a degree of latitude near the
#: equator. Deliberately the minimum and not the more familiar 111_320, which
#: is the equatorial *longitude* degree: dividing a radius by too large a
#: figure yields too small a span, and the box would clip real neighbours just
#: outside it. Measured: with 111_320 the north edge of a 1 km box lands 998.9 m
#: from the centre, so a property at 999.5 m would be silently dropped.
METRES_PER_DEGREE_MIN = 111_132.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def bounding_box(
    latitude: float, longitude: float, radius_m: float
) -> tuple[float, float, float, float]:
    """A lat/lon box that certainly contains everything within ``radius_m``.

    Exists so a nearest-neighbour query can be a plain indexed range scan in
    SQL and the exact distance computed in Python afterwards. SQLite only has
    trigonometric functions when compiled with SQLITE_ENABLE_MATH_FUNCTIONS,
    which is not guaranteed, so doing the haversine in the query would work on
    one machine and fail on another.

    The box is a superset, never a subset: the caller must still filter on the
    true distance. Longitude degrees shrink toward the poles, so the span is
    widened by 1/cos(latitude), clamped to keep it finite.
    """
    lat_span = radius_m / METRES_PER_DEGREE_MIN
    cos_lat = max(math.cos(math.radians(latitude)), 0.01)
    lon_span = radius_m / (METRES_PER_DEGREE_MIN * cos_lat)
    return (latitude - lat_span, latitude + lat_span,
            longitude - lon_span, longitude + lon_span)
