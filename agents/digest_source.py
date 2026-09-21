"""Digest source agent: LinkedIn/Wellfound/BuiltIn job-alert digest emails
-> pooled+deduped (title, company) pairs -> domain-restricted web search
(agents/digest_search.py) -> tier-3-only capture -> write to postings.

A second, independent discovery source alongside Adzuna (agents/discovery.py)
-- see CLAUDE.md's "Phase 4: Secondary discovery via personal digest
emails" for the full design and the reasoning behind every choice below.
Reuses agents/fetchers.py's tiered fetch + confirm/extract (via
capture_from_search()), not a separate capture pipeline.

    uv run agents/digest_source.py

Every registered agents/digest_parsers/ parser is picked up automatically
-- adding a new platform later is "drop a new file in digest_parsers/",
nothing here needs to change.
"""

import asyncio
import logging
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents import digest_search
from agents.common import AgentRunTracker
from agents.digest_parsers import ParsedListing, all_parsers, normalized_listing_id
from agents.fetchers import (
    MAX_FALLBACK_CANDIDATES,
    PostingReference,
    capture_from_search,
)
from agents.fetchers import get_llm_error_count as fetchers_llm_error_count
from agents.fetchers import get_total_cost_usd as fetchers_cost_usd
from agents.gmail_client import search_html_messages
from db.db import fetch_last_agent_run, insert_posting, posting_exists
from models.schema import Posting

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)
logging.getLogger("claude_agent_sdk").setLevel(logging.WARNING)

SOURCE = "email_digest"
AGENT_NAME = "digest_source"
FALLBACK_LOOKBACK_HOURS = 24  # same fallback send_digest.py uses on its first-ever run


def _total_cost_usd() -> float:
    """Sums fetchers.py's counter (capture_from_search's confirm+extract
    calls, via claude_agent_sdk) and digest_search.py's counter (the
    search call, via the raw Anthropic SDK) -- two different Claude
    clients, same reasoning categorize.py's independent counter already
    established for this project."""
    return fetchers_cost_usd() + digest_search.get_total_cost_usd()


def _total_llm_errors() -> int:
    return fetchers_llm_error_count() + digest_search.get_llm_error_count()


def _since_last_run() -> datetime:
    """Windowed to digests since the last run -- same pattern as
    send_digest.py's _llm_error_line(): anchor on this agent's own last
    agent_runs row, falling back to a fixed lookback when there's no
    prior run to anchor to."""
    last_run = fetch_last_agent_run(AGENT_NAME)
    if last_run:
        return last_run.finished_at
    return datetime.now(timezone.utc) - timedelta(hours=FALLBACK_LOOKBACK_HOURS)


def collect_listings(since: datetime) -> list[ParsedListing]:
    """Runs every registered digest parser against matching emails since
    `since`, pooled together (not per-platform) -- callers dedupe this
    combined list, since the same posting frequently gets surfaced by more
    than one platform on the same day."""
    since_epoch = int(since.timestamp())
    listings: list[ParsedListing] = []

    for parser_cls in all_parsers():
        parser = parser_cls()
        try:
            bodies = search_html_messages(parser.SENDER_QUERY, since_epoch)
        except Exception:
            logger.exception("Failed to fetch %s digest emails -- skipping this platform", parser.PLATFORM_NAME)
            continue

        logger.info("%s: %d digest email(s) since %s", parser.PLATFORM_NAME, len(bodies), since.isoformat())
        for html in bodies:
            try:
                parsed = parser.parse(html)
            except Exception:
                logger.exception("Failed to parse a %s digest email -- skipping it", parser.PLATFORM_NAME)
                continue
            listings.extend(parsed)

    return listings


def dedupe_listings(listings: list[ParsedListing]) -> dict[str, ParsedListing]:
    """Keyed by normalized_listing_id -- the same hash used as
    postings.external_id, so this dedup and the DB's own uniqueness
    constraint agree on what "the same listing" means. First occurrence
    wins; postings.source stays unified 'email_digest' rather than
    per-platform regardless of which one won (see CLAUDE.md), since the
    same listing regularly appears in more than one platform's digest and
    attributing the DB row to just the first one would be arbitrary. The
    winning listing's .platform *is* still carried forward on the
    ParsedListing object itself, though -- used for logging and the
    end-of-run summary in main() -- it just reflects only the first
    platform that produced this listing, not every platform it appeared
    in."""
    deduped: dict[str, ParsedListing] = {}
    for listing in listings:
        key = normalized_listing_id(listing.title, listing.company)
        deduped.setdefault(key, listing)
    return deduped


ListingOutcome = Literal["written", "already_seen", "no_match"]


async def process_listing(external_id: str, listing: ParsedListing) -> ListingOutcome:
    """Returns "written" if a new posting was captured and written,
    "already_seen" if skipped as a repeat, or "no_match" if search+capture
    found nothing confirmable (see capture_from_search()'s docstring for
    why a miss here means skip entirely rather than write a degraded row).
    Used both for run.record() bookkeeping and the end-of-run per-platform
    summary in main()."""
    if posting_exists(SOURCE, external_id):
        logger.info("Skipping already-seen listing %s (%s)", external_id, listing.title)
        return "already_seen"

    query = f"{listing.title} {listing.company}"
    candidate_urls = await digest_search.search(query)
    walked = candidate_urls[:MAX_FALLBACK_CANDIDATES]
    logger.info(
        "Searched %r for %s (from %s), got %d result(s), walking %d: %s",
        query, listing.title, listing.platform, len(candidate_urls), len(walked), walked,
    )

    reference = PostingReference(
        title=listing.title, company=listing.company, location=listing.location
    )
    result = await capture_from_search(reference, candidate_urls)

    if result is None:
        logger.info("No candidate confirmed for %s at %s -- skipping", listing.title, listing.company)
        return "no_match"

    posting = Posting(
        source=SOURCE,
        external_id=external_id,
        title=listing.title,
        company=listing.company,
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
        external_id, result.description_source, result.work_location,
    )
    logger.info("Running total Claude API cost so far: $%.4f", _total_cost_usd())
    return "written"


def _log_summary(counts: dict[str, Counter]) -> None:
    """Per-platform breakdown of how every listing in this run's dedup set
    resolved -- written, already-seen (not a failure, just a repeat), no
    match found, or errored outright. Logged as the last thing this agent
    does, so it's easy to find at the end of this stage's GitHub Actions
    log without needing a separate step or a way to pass data between
    run_pipeline.py's subprocess stages (each stage runs as its own
    process, so a truly separate step would have to re-query the DB
    rather than reuse these in-memory counts)."""
    logger.info("=== Digest source summary ===")
    total = Counter()
    for platform in sorted(counts):
        c = counts[platform]
        total.update(c)
        this_total = sum(c.values())
        logger.info(
            "%s: %d written, %d already seen, %d no match, %d error(s) (%d total)",
            platform, c["written"], c["already_seen"], c["no_match"], c["error"], this_total,
        )
    grand_total = sum(total.values())
    logger.info(
        "TOTAL: %d written, %d already seen, %d no match, %d error(s) (%d total)",
        total["written"], total["already_seen"], total["no_match"], total["error"], grand_total,
    )


async def main() -> None:
    since = _since_last_run()
    counts: dict[str, Counter] = defaultdict(Counter)

    async with AgentRunTracker(AGENT_NAME) as run:
        listings = collect_listings(since)
        deduped = dedupe_listings(listings)
        logger.info(
            "%d listing(s) across all platforms, %d after cross-platform dedup",
            len(listings), len(deduped),
        )

        for external_id, listing in deduped.items():
            try:
                outcome = await process_listing(external_id, listing)
                counts[listing.platform][outcome] += 1
                if outcome == "written":
                    run.record(is_new=True)
            except Exception as e:
                logger.exception("Failed to process listing %s -- skipping", listing.title)
                run.record_error(f"{external_id} ({listing.title}): {e}")
                counts[listing.platform]["error"] += 1
        run.llm_errors = _total_llm_errors()

    _log_summary(counts)
    logger.info("Run complete. Total Claude API cost: $%.4f", _total_cost_usd())


if __name__ == "__main__":
    asyncio.run(main())
