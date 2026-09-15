"""Scraper interface. Each site subclasses this; the orchestrator only ever
sees ``discover()``."""
from __future__ import annotations

import abc
from typing import AsyncIterator

from app.core.http_client import HttpClient
from app.core.robots import RobotsGate
from app.models.property import ListingSource, PropertyListing


class BaseScraper(abc.ABC):
    source: ListingSource

    def __init__(self, http: HttpClient, robots: RobotsGate | None = None) -> None:
        self.http = http
        self.robots = robots or RobotsGate(http)

    @abc.abstractmethod
    def discover(self, **filters) -> AsyncIterator[PropertyListing]:
        """Yield newly listed properties, newest first.

        Implementations must check ``self.robots`` before every fetch, respect
        the rate limits configured for their host, and pause between detail
        pages. Prefer a published machine-readable interface — a sitemap, a
        feed, schema.org JSON-LD — over parsing page markup: it is both kinder
        to the site and far less likely to break.
        """
