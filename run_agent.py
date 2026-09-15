#!/usr/bin/env python3
"""Phase 1 entrypoint: python run_agent.py [--once]"""
from __future__ import annotations

import argparse
import asyncio
import logging

from app.core.config import settings
from app.core.http_client import HttpClient
from app.core.logging import setup_logging
from app.db.repository import PropertyRepository
from app.pipeline.orchestrator import Orchestrator
from app.services.region_filter import parse_regions


async def main(once: bool) -> None:
    setup_logging()
    region_filter = parse_regions(settings.regions)
    if not region_filter.is_empty:
        logging.getLogger("run_agent").info("region filter active: %s", region_filter)
    async with PropertyRepository(settings.database_url) as repo, HttpClient() as http:
        orchestrator = Orchestrator(http, repo, region_filter=region_filter)
        if once:
            sent = await orchestrator.run_once()
            logging.getLogger("run_agent").info(
                "cycle complete: %d alert(s) sent; store now holds %s", sent, await repo.stats()
            )
        else:
            await orchestrator.run_forever()


def cli() -> None:
    """Console entry point: ``woonagent`` (see pyproject [project.scripts])."""
    parser = argparse.ArgumentParser(description="Run the WoonAgent watch loop.")
    parser.add_argument("--once", action="store_true", help="Run a single cycle and exit.")
    asyncio.run(main(parser.parse_args().once))


if __name__ == "__main__":
    cli()
