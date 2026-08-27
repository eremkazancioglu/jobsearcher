"""Discovery agent: Adzuna search -> dedup check -> capture -> write.

Run manually while Phase 1's logic is validated (see CLAUDE.md's
Scheduling section) -- not on a schedule yet.

    uv run agents/discovery.py --what "data scientist" --where "united states"

Add --count-only to just see how many postings match, with no capture and
no DB writes:

    uv run agents/discovery.py --what "data scientist" --where "united states" --count-only

Add --list-only to see each matching posting's title and company (up to
--results-per-page of them), with no capture and no DB writes:

    uv run agents/discovery.py --what "data scientist" --where "united states" --list-only

By default, paginates through up to --max-pages (5) of results, not just
one page -- narrow this with --title-only / --max-days-old for a run
that's meant to run automatically, since each page is one Adzuna API hit
against a 250/day rate limit.
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.fetchers import capture, get_total_cost_usd
from db.db import insert_posting, posting_exists
from mcp_servers.job_sources.server import adzuna_count, adzuna_search
from models.schema import Posting

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)
# Quiet claude_agent_sdk's own INFO-level noise (e.g. "Using bundled Claude
# Code CLI: ...") -- not useful at our log level, drowns out our own logs.
logging.getLogger("claude_agent_sdk").setLevel(logging.WARNING)

SOURCE = "adzuna"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Adzuna discovery + full posting capture")
    parser.add_argument("--what", default="data scientist", help="Keyword(s) to search for")
    parser.add_argument("--where", default="", help="Location to search in")
    parser.add_argument("--salary-min", type=float, default=None)
    parser.add_argument("--max-days-old", type=int, default=14)
    parser.add_argument("--results-per-page", type=int, default=20)
    parser.add_argument(
        "--max-pages",
        type=int,
        default=5,
        help=(
            "Max Adzuna API pages to fetch (each page is one rate-limited hit -- "
            "Adzuna's daily cap is 250). Stops earlier if a page comes back short."
        ),
    )
    parser.add_argument(
        "--title-only", default=None, help="Restrict search to this phrase appearing in the title"
    )
    parser.add_argument(
        "--full-time", type=int, default=1, choices=[0, 1], help="1 for full-time only, 0 for no filter"
    )
    parser.add_argument(
        "--count-only",
        action="store_true",
        help="Just log the total number of matching postings and exit -- no capture, no DB writes.",
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="Just log each matching posting's title and company and exit -- no capture, no DB writes.",
    )
    return parser.parse_args()


def _log_capacity_warning(total: int, args: argparse.Namespace) -> None:
    """Warn if --results-per-page x --max-pages can't cover `total`
    matches. Called from --count-only as an upfront estimate (no pages are
    actually fetched there, so this is capacity, not a guarantee) -- useful
    for sanity-checking settings before a real run, especially a scheduled
    one where you expect the count to normally stay small."""
    capacity = args.results_per_page * args.max_pages
    if total > capacity:
        logger.info(
            "%d matching postings exceeds current capacity of %d "
            "(--results-per-page=%d x --max-pages=%d) -- a real run would not cover all of them",
            total, capacity, args.results_per_page, args.max_pages,
        )


def fetch_all_pages(args: argparse.Namespace) -> list:
    """Paginate through Adzuna results up to --max-pages, stopping earlier
    if a page comes back short (the last page). Logs the true total
    (adzuna_count -- one cheap extra hit) and explicitly flags when
    --max-pages means some matches won't be covered, rather than silently
    capping coverage -- same "no silent caps" pattern as tier 3's
    candidate walk in fetchers.py."""
    total = adzuna_count(
        what=args.what,
        where=args.where,
        max_days_old=args.max_days_old,
        salary_min=args.salary_min,
        title_only=args.title_only,
        full_time=args.full_time,
    )
    logger.info("Total matching postings for what=%r where=%r: %d", args.what, args.where, total)

    results = []
    for page in range(1, args.max_pages + 1):
        page_results = adzuna_search(
            what=args.what,
            where=args.where,
            max_days_old=args.max_days_old,
            salary_min=args.salary_min,
            results_per_page=args.results_per_page,
            title_only=args.title_only,
            full_time=args.full_time,
            page=page,
        )
        logger.info("Page %d: %d result(s)", page, len(page_results))
        results.extend(page_results)
        if len(page_results) < args.results_per_page:
            break  # short page -- no more results beyond this one

    covered = min(len(results), total)
    if covered < total:
        logger.info(
            "Covered %d of %d matching postings -- %d beyond --max-pages=%d were not fetched",
            covered, total, total - covered, args.max_pages,
        )
    return results


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
        work_location=result.work_location,
    )
    insert_posting(posting)
    logger.info(
        "Wrote posting %s (description_source=%s, work_location=%s)",
        adzuna.external_id, result.description_source, result.work_location,
    )
    logger.info("Running total Claude API cost so far: $%.4f", get_total_cost_usd())


async def main() -> None:
    args = parse_args()

    if args.count_only:
        count = adzuna_count(
            what=args.what,
            where=args.where,
            max_days_old=args.max_days_old,
            salary_min=args.salary_min,
            title_only=args.title_only,
            full_time=args.full_time,
        )
        logger.info("Total matching postings for what=%r where=%r: %d", args.what, args.where, count)
        _log_capacity_warning(count, args)
        return

    results = fetch_all_pages(args)

    if args.list_only:
        for adzuna in results:
            logger.info("%s -- %s", adzuna.title, adzuna.company)
        return

    for adzuna in results:
        try:
            await process_result(adzuna)
        except Exception:
            logger.exception("Failed to process posting %s -- skipping", adzuna.external_id)

    logger.info("Run complete. Total Claude API cost: $%.4f", get_total_cost_usd())


if __name__ == "__main__":
    asyncio.run(main())
