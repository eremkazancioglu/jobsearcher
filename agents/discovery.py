"""Discovery agent: Adzuna search -> dedup check -> capture -> write.

Run manually while Phase 1's logic is validated (see CLAUDE.md's
Scheduling section) -- not on a schedule yet.

    uv run agents/discovery.py --what "data scientist" --where "united states"
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.fetchers import capture
from db.db import insert_posting, posting_exists
from mcp_servers.job_sources.server import adzuna_search
from models.schema import Posting

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

SOURCE = "adzuna"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Adzuna discovery + full posting capture")
    parser.add_argument("--what", default="data scientist", help="Keyword(s) to search for")
    parser.add_argument("--where", default="", help="Location to search in")
    parser.add_argument("--salary-min", type=float, default=None)
    parser.add_argument("--max-days-old", type=int, default=14)
    parser.add_argument("--results-per-page", type=int, default=20)
    return parser.parse_args()


async def process_result(adzuna) -> None:
    if posting_exists(SOURCE, adzuna.external_id):
        logger.info("Skipping already-seen posting %s (%s)", adzuna.external_id, adzuna.title)
        return

    logger.info("Capturing %s at %s (%s)", adzuna.title, adzuna.company, adzuna.external_id)
    result = await capture(adzuna)

    posting = Posting(
        source=SOURCE,
        external_id=adzuna.external_id,
        title=adzuna.title,
        company=adzuna.company,
        location=adzuna.location,
        url=result.url,
        description=result.description,
        description_source=result.description_source,
        salary_min=result.salary_min,
        salary_max=result.salary_max,
        salary_is_predicted=result.salary_is_predicted,
    )
    insert_posting(posting)
    logger.info(
        "Wrote posting %s (description_source=%s)", adzuna.external_id, result.description_source
    )


async def main() -> None:
    args = parse_args()

    results = adzuna_search(
        what=args.what,
        where=args.where,
        max_days_old=args.max_days_old,
        salary_min=args.salary_min,
        results_per_page=args.results_per_page,
    )
    logger.info("Adzuna returned %d results for what=%r where=%r", len(results), args.what, args.where)

    for adzuna in results:
        try:
            await process_result(adzuna)
        except Exception:
            logger.exception("Failed to process posting %s -- skipping", adzuna.external_id)


if __name__ == "__main__":
    asyncio.run(main())
