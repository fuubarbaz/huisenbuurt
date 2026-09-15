"""Scraper tests — offline, against fixture markup.

The parser is deliberately pure (`HuispediaScraper.parse`), so the interesting
logic is testable without touching the network.
"""
from __future__ import annotations

import gzip
from datetime import date, datetime, timedelta, timezone

import pytest

from app.models.property import ListingSource
from app.scrapers.funda import FundaAccessNotPermitted, FundaScraper
from app.scrapers.huispedia import (
    URL_PATH_RE,
    HuispediaScraper,
    _listed_at,
    _split_house_number,
)

URL = "https://huispedia.nl/s-hertogenbosch/5211rw/tolbrugstraat/3"

JSONLD = """
<script type="application/ld+json">[
 {"@context":"http://schema.org","@type":"SingleFamilyResidence",
  "name":"Tolbrugstraat 3, 's-Hertogenbosch",
  "address":{"@type":"PostalAddress","streetAddress":"Tolbrugstraat 3",
             "addressLocality":"'s-Hertogenbosch","postalCode":"5211 RW"},
  "geo":{"@type":"GeoCoordinates","latitude":51.691,"longitude":5.304},
  "floorSize":81,"numberOfRooms":3},
 {"@context":"http://schema.org/","@type":"Product",
  "offers":{"@type":"Offer","price":625000,"priceCurrency":"EUR"}}
]</script>
"""

FEATURES = """
<li class="feature-item"><span class="name">Vraagprijs</span><span class="value">€ 625.000</span></li>
<li class="feature-item"><span class="name">Bouwjaar</span><span class="value">2026</span></li>
<li class="feature-item"><span class="name">Woonoppervlakte</span><span class="value">81 m2</span></li>
<li class="feature-item"><span class="name">Aantal kamers</span><span class="value">3 kamers</span></li>
<li class="feature-item"><span class="name">Aangeboden sinds</span><span class="value">7 uur</span></li>
"""

PAGE = f"<html><body>{JSONLD}{FEATURES}</body></html>"


def parts(url: str = URL) -> dict[str, str]:
    m = URL_PATH_RE.match(url)
    assert m, url
    return m.groupdict()


# -- URL shape ---------------------------------------------------------------


def test_url_carries_postcode_and_number():
    p = parts()
    assert p["pc6"] == "5211rw"
    assert p["number"] == "3"
    assert p["city"] == "s-hertogenbosch"


def test_non_listing_urls_are_rejected():
    for bad in ["https://huispedia.nl/amsterdam",
                "https://huispedia.nl/amsterdam/1015aa/singel",
                "https://huispedia.nl/help/contact"]:
        assert URL_PATH_RE.match(bad) is None


def test_house_number_and_addition_are_separated():
    assert _split_house_number("289-b") == ("289", "b")
    assert _split_house_number("43-15") == ("43", "15")
    assert _split_house_number("3") == ("3", None)


def test_property_id_is_stable_and_address_derived():
    first = HuispediaScraper.property_id_for(URL)
    assert first == "huispedia:s-hertogenbosch/5211rw/tolbrugstraat/3"
    # Trailing slash and case must not produce a second id for one house.
    assert HuispediaScraper.property_id_for(URL + "/") == first
    assert HuispediaScraper.property_id_for(URL.upper().replace("HTTPS", "https")) == first


# -- parsing -----------------------------------------------------------------


def test_structured_data_supplies_the_facts():
    listing = HuispediaScraper.parse(URL, PAGE, parts())

    assert listing is not None
    assert listing.source is ListingSource.HUISPEDIA
    assert listing.postal_code == "5211RW"          # normalised, space removed
    assert listing.house_number == "3"
    assert listing.price_eur == 625_000
    assert listing.living_area_m2 == 81
    assert listing.rooms == 3
    assert listing.construction_year == 2026
    assert listing.city == "'s-Hertogenbosch"


def test_construction_year_comes_from_the_feature_list():
    """It is absent from the JSON-LD, and the foundation layer needs it."""
    without = HuispediaScraper.parse(URL, f"<html>{JSONLD}</html>", parts())
    assert without is not None and without.construction_year is None

    with_features = HuispediaScraper.parse(URL, PAGE, parts())
    assert with_features.construction_year == 2026


def test_price_falls_back_to_the_feature_list():
    """Dutch thousands separators are dots: '€ 625.000' is not 625."""
    no_jsonld_price = f"<html>{FEATURES}</html>"
    listing = HuispediaScraper.parse(URL, no_jsonld_price, parts())

    assert listing is not None
    assert listing.price_eur == 625_000


def test_page_without_structured_data_still_yields_an_address():
    listing = HuispediaScraper.parse(URL, "<html><body>nothing</body></html>", parts())

    assert listing is not None
    assert listing.postal_code == "5211RW"          # recovered from the URL
    assert listing.price_eur is None
    assert "Tolbrugstraat 3" in listing.address


def test_malformed_json_ld_does_not_raise():
    broken = '<script type="application/ld+json">{not json</script>'
    listing = HuispediaScraper.parse(URL, broken, parts())
    assert listing is not None


def test_unparseable_postcode_yields_none_rather_than_a_bad_listing():
    bad = dict(parts(), pc6="99")
    assert HuispediaScraper.parse("https://huispedia.nl/x/99/y/1", "<html></html>", bad) is None


# -- "aangeboden sinds" ------------------------------------------------------


def test_relative_listing_age_is_anchored_to_an_absolute_time():
    """The site says how long it has been up, not when it went up."""
    now = datetime.now(timezone.utc)
    assert (now - _listed_at("7 uur")) == pytest.approx(timedelta(hours=7), abs=timedelta(seconds=5))
    assert (now - _listed_at("3 dagen")) == pytest.approx(timedelta(days=3), abs=timedelta(seconds=5))
    assert (now - _listed_at("2 weken")) == pytest.approx(timedelta(weeks=2), abs=timedelta(seconds=5))
    assert _listed_at(None) is None
    assert _listed_at("onbekend") is None


# -- discovery ---------------------------------------------------------------


SITEMAP_XML = """<?xml version="1.0"?><urlset>
<url><loc>https://huispedia.nl/a/1015aa/x/1</loc><lastmod>2026-09-05</lastmod></url>
<url><loc>https://huispedia.nl/b/1015ab/y/2</loc><lastmod>2026-08-01</lastmod></url>
<url><loc>https://huispedia.nl/c/1015ac/z/3</loc><lastmod>2026-09-04</lastmod></url>
</urlset>"""


class FakeResponse:
    def __init__(self, content): self.content = content


class FakeHttp:
    def __init__(self, index, sitemap_bytes):
        self.index, self.sitemap_bytes, self.fetched = index, sitemap_bytes, []

    async def get_text(self, url, **kw):
        self.fetched.append(url)
        return self.index

    async def request(self, method, url, **kw):
        self.fetched.append(url)
        return FakeResponse(self.sitemap_bytes)


class AllowAll:
    async def require(self, url): return None
    async def allows(self, url): return True


async def test_discovery_filters_on_lastmod():
    index = "<loc>https://huispedia.nl/sitemaps/properties-listed-0046.xml.gz</loc>"
    http = FakeHttp(index, gzip.compress(SITEMAP_XML.encode()))
    scraper = HuispediaScraper.__new__(HuispediaScraper)
    scraper.http, scraper.robots = http, AllowAll()

    urls = await scraper._recent_urls(date(2026, 9, 4), sitemaps_to_scan=3)

    assert "https://huispedia.nl/b/1015ab/y/2" not in urls   # August, too old
    assert len(urls) == 2


async def test_only_listed_sitemaps_are_scanned():
    index = ("<loc>https://huispedia.nl/sitemaps/properties-addresses-0001.xml.gz</loc>"
             "<loc>https://huispedia.nl/sitemaps/properties-listed-0046.xml.gz</loc>")
    http = FakeHttp(index, gzip.compress(SITEMAP_XML.encode()))
    scraper = HuispediaScraper.__new__(HuispediaScraper)
    scraper.http, scraper.robots = http, AllowAll()

    await scraper._recent_urls(date(2026, 9, 4), sitemaps_to_scan=3)

    assert not any("properties-addresses" in u for u in http.fetched)


# -- Funda -------------------------------------------------------------------


async def test_funda_refuses_and_says_why():
    scraper = FundaScraper.__new__(FundaScraper)
    with pytest.raises(FundaAccessNotPermitted, match="anti-bot"):
        async for _ in scraper.discover():
            pass
