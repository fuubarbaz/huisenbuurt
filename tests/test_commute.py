"""Commute routing tests — offline."""
from __future__ import annotations

import pytest

from app.core.config import settings
from app.models.property import GeoIdentity
from app.services.commute import (
    MAX_DESTINATIONS,
    CommuteService,
    TravelMode,
)
from app.services.geo.pdok_locatieserver import AddressMismatch, _covers_query

ORIGIN = GeoIdentity(latitude=52.3005, longitude=4.8607,
                     matched_address="Bos en Vaartlaan 1, 1181AA Amstelveen")
TARGET = GeoIdentity(latitude=52.3789, longitude=4.9003,
                     matched_address="Metrostation Centraal Station, Amsterdam")

OSRM_OK = {"routes": [{"duration": 1356.0, "distance": 14700.0}]}
BROUTER_OK = {"features": [{"properties": {"total-time": "1494", "track-length": "8700"}}]}


class FakeHttp:
    """Routes by URL so each mode can be scripted independently."""

    def __init__(self, osrm=OSRM_OK, brouter=BROUTER_OK, ors=None):
        self.osrm, self.brouter, self.ors = osrm, brouter, ors
        self.urls = []

    async def get_json(self, url, **kw):
        self.urls.append(url)
        payload = (self.osrm if "project-osrm" in url
                   else self.brouter if "brouter" in url else self.ors)
        if isinstance(payload, Exception):
            raise payload
        if payload is None:
            raise RuntimeError("not configured")
        return payload


class FakeGeocoder:
    def __init__(self, by_query): self.by_query = by_query

    async def search(self, query):
        result = self.by_query.get(query, TARGET)
        if isinstance(result, Exception):
            raise result
        return result


def service(http=None, geocoder=None) -> CommuteService:
    s = CommuteService.__new__(CommuteService)
    s.http = http or FakeHttp()
    s.geocoder = geocoder or FakeGeocoder({})
    return s


@pytest.fixture(autouse=True)
def no_transit_key(monkeypatch):
    monkeypatch.setattr(settings, "ors_api_key", None)


# -- the two routers ---------------------------------------------------------


async def test_car_and_bike_come_from_different_routers():
    """OSRM's public server answers every profile from its driving graph, so a
    bike time taken from it would be a car time with another label."""
    result = await service().travel_times(ORIGIN, [{"label": "Work", "query": "x"}])
    legs = {leg.mode: leg for leg in result.destinations[0].legs}

    assert legs[TravelMode.CAR].router == "OSRM"
    assert legs[TravelMode.BIKE].router == "BRouter"
    assert legs[TravelMode.CAR].distance_km != legs[TravelMode.BIKE].distance_km


async def test_durations_are_converted_to_minutes():
    result = await service().travel_times(ORIGIN, [{"label": "Work", "query": "x"}])
    legs = {leg.mode: leg for leg in result.destinations[0].legs}

    assert legs[TravelMode.CAR].duration_min == pytest.approx(22.6, abs=0.1)
    assert legs[TravelMode.BIKE].duration_min == pytest.approx(24.9, abs=0.1)
    assert legs[TravelMode.CAR].distance_km == pytest.approx(14.7, abs=0.1)


async def test_one_router_failing_leaves_the_other():
    http = FakeHttp(osrm=RuntimeError("osrm down"))
    result = await service(http).travel_times(ORIGIN, [{"label": "Work", "query": "x"}])
    modes = {leg.mode for leg in result.destinations[0].legs}

    assert TravelMode.CAR not in modes
    assert TravelMode.BIKE in modes


async def test_an_empty_route_response_yields_no_leg():
    http = FakeHttp(osrm={"routes": []}, brouter={"features": []})
    result = await service(http).travel_times(ORIGIN, [{"label": "Work", "query": "x"}])

    assert result.destinations[0].legs == []


# -- transit -----------------------------------------------------------------


async def test_transit_is_absent_without_a_key():
    result = await service().travel_times(ORIGIN, [{"label": "Work", "query": "x"}])

    assert result.transit_available is False
    assert not any(leg.mode is TravelMode.TRANSIT for leg in result.destinations[0].legs)


async def test_transit_is_used_when_a_key_is_configured(monkeypatch):
    monkeypatch.setattr(settings, "ors_api_key", "test-key")
    ors = {"features": [{"properties": {"summary": {"duration": 2400, "distance": 16000}}}]}
    result = await service(FakeHttp(ors=ors)).travel_times(
        ORIGIN, [{"label": "Work", "query": "x"}])
    leg = result.destinations[0].leg(TravelMode.TRANSIT)

    assert result.transit_available is True
    assert leg.duration_min == pytest.approx(40.0)


# -- destinations ------------------------------------------------------------


async def test_several_destinations_are_all_returned():
    dests = [{"label": f"D{i}", "query": f"q{i}"} for i in range(3)]
    result = await service().travel_times(ORIGIN, dests)

    assert [d.label for d in result.destinations] == ["D0", "D1", "D2"]


async def test_the_destination_count_is_capped():
    dests = [{"label": f"D{i}", "query": f"q{i}"} for i in range(MAX_DESTINATIONS + 4)]
    result = await service().travel_times(ORIGIN, dests)

    assert len(result.destinations) == MAX_DESTINATIONS


async def test_a_bad_destination_does_not_lose_the_good_ones():
    geocoder = FakeGeocoder({"nowhere": AddressMismatch("no confident match")})
    result = await service(geocoder=geocoder).travel_times(ORIGIN, [
        {"label": "Good", "query": "somewhere"},
        {"label": "Bad", "query": "nowhere"},
    ])

    assert result.destinations[0].legs
    assert result.destinations[1].legs == []
    assert "no confident match" in result.destinations[1].error


async def test_an_unfindable_destination_says_so():
    geocoder = FakeGeocoder({"ghost": None})
    result = await service(geocoder=geocoder).travel_times(
        ORIGIN, [{"label": "Ghost", "query": "ghost"}])

    assert "could not find" in result.destinations[0].error


async def test_an_empty_query_is_rejected_without_routing():
    http = FakeHttp()
    result = await service(http).travel_times(ORIGIN, [{"label": "Blank", "query": "  "}])

    assert result.destinations[0].error == "no destination given"
    assert http.urls == []


async def test_the_label_defaults_to_the_query():
    result = await service().travel_times(ORIGIN, [{"query": "Amsterdam Centraal"}])
    assert result.destinations[0].label == "Amsterdam Centraal"


# -- destination matching ----------------------------------------------------


def test_a_match_must_keep_the_distinctive_words():
    """Real failures: "Utrecht Centraal" resolves to a bus station in Breda,
    and "Schiphol Airport" to Maastricht. Both share one generic word."""
    assert not _covers_query("Utrecht Centraal", "Centraal Busstation, Breda")
    assert not _covers_query("Schiphol Airport", "Maastricht-Airport, Beek, Limburg")
    assert _covers_query("Amsterdam Centraal", "Metrostation Centraal Station, Amsterdam")
    assert _covers_query("Utrecht", "Gemeente Utrecht")


def test_generic_words_alone_never_carry_a_match():
    assert not _covers_query("Eindhoven station", "Centraal Station, Rotterdam")


def test_a_query_of_only_generic_words_is_not_rejected():
    """Nothing distinctive was asked for, so there is nothing to contradict."""
    assert _covers_query("station", "Centraal Station, Rotterdam")
