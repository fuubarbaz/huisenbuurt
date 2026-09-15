"""Market-pressure tests — offline.

The headline risk here is a claim the data cannot support, so several of these
pin what the numbers are *not*.
"""
from __future__ import annotations

import pytest

from app.models.market import MarketPoint, MarketTrend
from app.models.woz import WozPoint, WozTrend
from app.services.market import SALE_PRICE_FIELD, MarketLookup


class FakeHttp:
    def __init__(self, sales, fail=False):
        self.sales, self.fail = sales, fail
        self.filters = []

    async def get_json(self, url, **kw):
        if self.fail:
            raise RuntimeError("cbs down")
        self.filters.append(kw.get("params", {}).get("$filter"))
        return {"value": [{"RegioS": "GM0362", "Perioden": f"{y}JJ00", SALE_PRICE_FIELD: v}
                          for y, v in self.sales.items()]}


class FakeWoz:
    def __init__(self, by_year, fail=False): self.by_year, self.fail = by_year, fail

    async def trend(self, area, years=7):
        if self.fail:
            raise RuntimeError("woz down")
        return WozTrend(area_code=area, area_level="gemeente",
                        points=[WozPoint(year=y, value_eur=v) for y, v in sorted(self.by_year.items())])


def lookup(sales, woz, *, sales_fail=False, woz_fail=False) -> MarketLookup:
    m = MarketLookup.__new__(MarketLookup)
    m.http = FakeHttp(sales, sales_fail)
    m.base_url = "https://example.invalid"
    m.woz = FakeWoz(woz, woz_fail)
    return m


# The real Amstelveen series: the 2021-22 peak is the overbidding era.
SALES = {2019: 525_247, 2020: 534_717, 2021: 625_658, 2022: 658_372,
         2023: 615_382, 2024: 649_823, 2025: 681_691}
WOZ = {2019: 384_000, 2020: 420_000, 2021: 442_000, 2022: 470_000,
       2023: 527_000, 2024: 536_000, 2025: 568_000}


# -- the ratio ---------------------------------------------------------------


async def test_the_ratio_tracks_the_real_market_cycle():
    trend = await lookup(SALES, WOZ).trend("GM0362", years=7)

    peak = trend.pressure_peak
    peak_year = next(p.year for p in trend.points if p.sale_to_woz == peak)
    assert peak_year in (2021, 2022)            # the overbidding years
    assert trend.pressure_now == pytest.approx(1.20, abs=0.01)
    assert peak > trend.pressure_now            # the market cooled


async def test_a_missing_half_of_the_pair_yields_no_ratio():
    trend = await lookup({2025: 500_000}, {}).trend("GM0362", years=3)

    assert trend.points[-1].sale_to_woz is None
    assert trend.pressure_now is None


async def test_the_ratio_survives_serialisation():
    dumped = MarketPoint(year=2025, average_sale_eur=600_000,
                         average_woz_eur=500_000).model_dump()
    assert dumped["sale_to_woz"] == pytest.approx(1.2)


# -- comparing this house to the area ----------------------------------------


async def test_asking_price_is_compared_with_the_latest_area_average():
    trend = await lookup(SALES, WOZ).trend("GM0362", years=7, asking_price_eur=650_000)

    # 650,000 against a 681,691 average is about 4.6% below.
    assert trend.asking_vs_area_pct == pytest.approx(-4.6, abs=0.2)


async def test_an_expensive_listing_reads_positive():
    trend = await lookup(SALES, WOZ).trend("GM0362", years=7, asking_price_eur=900_000)
    assert trend.asking_vs_area_pct > 0


async def test_no_asking_price_means_no_comparison():
    trend = await lookup(SALES, WOZ).trend("GM0362", years=7)
    assert trend.asking_vs_area_pct is None


async def test_sale_price_growth_is_measured_across_the_span():
    trend = await lookup(SALES, WOZ).trend("GM0362", years=7)
    # 525,247 -> 681,691
    assert trend.sale_price_change_pct == pytest.approx(29.8, abs=0.2)


# -- resilience --------------------------------------------------------------


async def test_a_failing_sale_lookup_still_yields_the_woz_series():
    trend = await lookup(SALES, WOZ, sales_fail=True).trend("GM0362", years=3)

    assert trend.points
    assert all(p.average_sale_eur is None for p in trend.points)
    assert trend.pressure_now is None


async def test_a_failing_woz_lookup_still_yields_sale_prices():
    trend = await lookup(SALES, WOZ, woz_fail=True).trend("GM0362", years=3)

    assert any(p.average_sale_eur for p in trend.points)
    assert trend.pressure_now is None


async def test_no_gemeente_code_returns_an_empty_trend():
    trend = await lookup(SALES, WOZ).trend("")
    assert trend.points == [] and trend.gemeente_code is None


# -- the CBS key convention --------------------------------------------------


async def test_regios_is_queried_unpadded():
    """Third convention in the codebase: WijkenEnBuurten pads to 10,
    SoortMisdrijf to 6, RegioS here not at all. A wrong width returns [] with
    a 200 rather than an error."""
    lk = lookup(SALES, WOZ)
    await lk.trend("GM0362", years=3)

    assert "RegioS eq 'GM0362'" in lk.http.filters[0]
    assert "GM0362 " not in lk.http.filters[0]


# -- what this is not --------------------------------------------------------


def test_the_model_does_not_claim_to_be_an_overbid_percentage():
    """Guards the naming: nothing here should read as a measured overbid."""
    fields = set(MarketTrend.model_fields) | {
        n for n in dir(MarketTrend) if not n.startswith("_")}
    for name in fields:
        assert "overbid" not in name.lower()
        assert "overbieden" not in name.lower()
