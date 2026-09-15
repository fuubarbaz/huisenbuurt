"""Structured-ish logging setup, called once from the entrypoints."""
from __future__ import annotations

import logging
import sys

from app.core.config import settings

_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def setup_logging(level: str | None = None) -> None:
    logging.basicConfig(
        level=getattr(logging, (level or settings.log_level).upper(), logging.INFO),
        format=_FORMAT,
        stream=sys.stdout,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
