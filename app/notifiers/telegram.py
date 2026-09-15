"""Telegram Bot API delivery. Knows nothing about scoring or scraping."""
from __future__ import annotations

import logging

from app.core.config import settings
from app.core.http_client import HttpClient, TransientHTTPError
from app.models.score import ScoredProperty
from app.notifiers.base import Notifier
from app.notifiers.formatters.markdown_card import render_card

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
MAX_MESSAGE_CHARS = 4096


class TelegramNotifier(Notifier):
    def __init__(
        self,
        http: HttpClient,
        *,
        bot_token: str | None = None,
        chat_id: str | None = None,
    ) -> None:
        self.http = http
        self.bot_token = bot_token or settings.telegram_bot_token
        self.chat_id = chat_id or settings.telegram_chat_id
        if not self.bot_token or not self.chat_id:
            raise ValueError("TelegramNotifier needs a bot token and a chat id")

    async def send(self, item: ScoredProperty) -> bool:
        text = render_card(item)[:MAX_MESSAGE_CHARS]
        try:
            await self.http.request(
                "POST",
                f"{TELEGRAM_API}/bot{self.bot_token}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "MarkdownV2",
                    "disable_web_page_preview": False,
                },
            )
        except TransientHTTPError as exc:
            log.error("telegram delivery failed for %s: %s", item.listing.property_id, exc)
            return False
        return True
