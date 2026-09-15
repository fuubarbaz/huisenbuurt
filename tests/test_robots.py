"""robots.txt gate tests. The failure modes are the point."""
from __future__ import annotations

import pytest

from app.core.robots import RobotsDisallowed, RobotsGate

HUISPEDIA = """Sitemap: https://huispedia.nl/sitemap.xml

User-agent: *
Disallow: /api/tracking/
Disallow: /contact
User-agent: AhrefsBot
Disallow: /
"""

# What funda.nl actually returns for /robots.txt.
ANTI_BOT_PAGE = "<!doctype html><html lang=\"nl\"><head><title>Je bent bijna op de pagina die je zoekt</title></head><body>" + "x" * 500 + "</body></html>"


class FakeHttp:
    def __init__(self, body): self.body, self.calls = body, 0

    async def get_text(self, url, **kw):
        self.calls += 1
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


def gate(body) -> RobotsGate:
    return RobotsGate(FakeHttp(body), user_agent="WoonAgent/0.1")


async def test_permitted_path_is_allowed():
    assert await gate(HUISPEDIA).allows("https://huispedia.nl/amsterdam/1015aa/singel/30")


async def test_disallowed_path_is_refused():
    assert not await gate(HUISPEDIA).allows("https://huispedia.nl/contact")


async def test_anti_bot_page_is_refused_not_read_as_no_rules():
    """The critical case: an HTML challenge is a refusal, not an absent policy."""
    assert not await gate(ANTI_BOT_PAGE).allows("https://www.funda.nl/koop/amsterdam/")


async def test_network_failure_fails_closed():
    """An unreachable policy is not permission."""
    assert not await gate(RuntimeError("connection reset")).allows("https://example.nl/x")


async def test_require_raises_with_the_url():
    with pytest.raises(RobotsDisallowed, match="huispedia.nl/contact"):
        await gate(HUISPEDIA).require("https://huispedia.nl/contact")


async def test_rules_are_fetched_once_per_host():
    g = gate(HUISPEDIA)
    for path in ("/a/1015aa/x/1", "/b/1015ab/y/2", "/c/1015ac/z/3"):
        await g.allows(f"https://huispedia.nl{path}")

    assert g.http.calls == 1


async def test_sitemaps_are_exposed():
    assert await gate(HUISPEDIA).sitemaps("https://huispedia.nl/") == [
        "https://huispedia.nl/sitemap.xml"
    ]


async def test_no_sitemap_advertised_returns_empty():
    assert await gate("User-agent: *\nDisallow:\n").sitemaps("https://x.nl/") == []
