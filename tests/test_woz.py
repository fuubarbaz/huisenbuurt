"""WOZ valuation-trend tests — offline.

The interesting behaviour is what happens when CBS renumbers an area: the
series must show a gap rather than stitching two different geographies
together and calling the result a trend.
"""
from __future__ import annotations

import pytest

from app.models.woz import WozPoint, WozTrend
from app.services.woz import KWB_EDITIONS, WOZ_FIELD, WozLookup

AREA = "BU03620102"


class FakeHttp:
    """Serves a WOZ value per dataset; None means the area is absent there."""

    def __init__(self, by_dataset, properties=None):
        self.by_dataset, self.properties = by_dataset, properties
        self.property_calls = 0

    async def get_json(self, url, **kw):
        dataset = url.split("/")[-2]
        if url.endswith("DataProperties"):
            self.property_calls += 1
            return {"value": self.properties or []}
        value = self.by_dataset.get(dataset)
        if isinstance(value, Exception):
            raise value
        field = WOZ_FIELD.get(dataset, "GemiddeldeWOZWaardeVanWoningen_39")
        return {"value": [] if value is None else [{field: value}]}


def lookup(by_year, **kw) -> WozLookup:
    by_dataset = {KWB_EDITIONS[y]: v for y, v in by_year.items()}
    return WozLookup(FakeHttp(by_dataset, **kw))


# -- the series --------------------------------------------------------------


async def test_a_full_series_is_assembled_oldest_first():
    trend = await lookup({2023: 563, 2024: 563, 2025: 602}).trend(AREA, years=3)

    assert [p.year for p in trend.points] == [2023, 2024, 2025]
    assert trend.points[-1].value_eur == 602_000      # CBS publishes thousands
    assert trend.area_level == "buurt"


async def test_percentage_changes_are_computed_over_the_right_spans():
    trend = await lookup({2023: 500, 2024: 550, 2025: 600}).trend(AREA, years=3)

    assert trend.change_pct_1y == pytest.approx(9.1, abs=0.1)   # 550 -> 600
    assert trend.change_pct == pytest.approx(20.0, abs=0.1)     # 500 -> 600
    assert trend.latest.year == 2025


async def test_the_percentages_survive_serialisation():
    """They are computed fields — a bare @property never reaches the API."""
    trend = await lookup({2024: 500, 2025: 600}).trend(AREA, years=2)
    dumped = trend.model_dump()

    assert dumped["change_pct_1y"] == pytest.approx(20.0)
    assert dumped["change_pct"] == pytest.approx(20.0)


async def test_a_single_point_yields_no_percentages():
    trend = await lookup({2025: 602}).trend(AREA, years=1)

    assert trend.change_pct is None
    assert trend.change_pct_1y is None


# -- boundary changes --------------------------------------------------------


async def test_missing_years_are_recorded_as_gaps_not_zeros():
    """Amsterdam's codes really do vanish before the 2023 edition."""
    trend = await lookup({2021: None, 2022: None, 2023: 935,
                          2024: 906, 2025: 891}).trend(AREA, years=5)

    assert trend.missing_years == [2021, 2022]
    assert [p.year for p in trend.points] == [2023, 2024, 2025]
    assert all(p.value_eur > 0 for p in trend.points)


async def test_the_span_is_measured_over_published_years_only():
    """The change must not be computed against a year that has no figure."""
    trend = await lookup({2021: None, 2022: None, 2023: 900, 2025: 990}).trend(AREA, years=5)

    assert trend.change_pct == pytest.approx(10.0, abs=0.1)   # 900 -> 990


async def test_an_area_absent_everywhere_yields_an_empty_trend():
    trend = await lookup({2024: None, 2025: None}).trend(AREA, years=2)

    assert trend.points == []
    assert trend.latest is None
    assert trend.change_pct is None


async def test_no_area_code_is_handled():
    trend = await WozLookup(FakeHttp({})).trend("")
    assert trend.points == [] and trend.area_code is None


# -- resilience --------------------------------------------------------------


async def test_one_failing_edition_does_not_lose_the_rest():
    trend = await lookup({2023: 563, 2024: RuntimeError("cbs down"),
                          2025: 602}).trend(AREA, years=3)

    assert [p.year for p in trend.points] == [2023, 2025]
    assert 2024 in trend.missing_years


async def test_a_known_field_key_costs_no_discovery_call():
    lk = lookup({2025: 602})
    await lk.trend(AREA, years=1)

    assert lk.http.property_calls == 0


async def test_an_unknown_edition_discovers_its_field_and_memoises_it():
    """Discovery costs one call per unknown edition, per process — not per property."""
    unknown = KWB_EDITIONS[2016]
    WOZ_FIELD.pop(unknown, None)
    lk = lookup({2016: 250}, properties=[
        {"Key": "GemiddeldeWOZWaardeVanWoningen_31", "Title": "Gemiddelde WOZ-waarde van woningen"}])

    trend = await lk.trend(AREA, years=len(KWB_EDITIONS))
    first_pass = lk.http.property_calls

    assert first_pass >= 1
    assert WOZ_FIELD[unknown] == "GemiddeldeWOZWaardeVanWoningen_31"
    assert any(p.year == 2016 for p in trend.points)

    # Second property, same process: every key is now known.
    await lk.trend(AREA, years=len(KWB_EDITIONS))
    assert lk.http.property_calls == first_pass


def test_every_listed_edition_is_a_distinct_dataset():
    assert len(set(KWB_EDITIONS.values())) == len(KWB_EDITIONS)
    assert sorted(KWB_EDITIONS) == list(KWB_EDITIONS)     # already chronological
