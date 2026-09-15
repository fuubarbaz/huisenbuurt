"""Notifier interface. The pipeline depends on this, never on Telegram."""
from __future__ import annotations

import abc

from app.models.score import ScoredProperty


class Notifier(abc.ABC):
    @abc.abstractmethod
    async def send(self, item: ScoredProperty) -> bool:
        """Deliver one enriched property. Returns True on success."""


class NullNotifier(Notifier):
    """Dry-run sink used in tests and when settings.dry_run is set."""

    def __init__(self) -> None:
        self.sent: list[ScoredProperty] = []

    async def send(self, item: ScoredProperty) -> bool:
        self.sent.append(item)
        return True
