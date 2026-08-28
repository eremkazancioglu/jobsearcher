"""Slack digest agent: sends every strong/mixed match not yet digested, in
one message.

    uv run digest/send_digest.py

Falls back to printing to stdout if SLACK_WEBHOOK_URL isn't set in .env --
useful for local runs without a real webhook configured, per CLAUDE.md's
Tools and stack section.

Reads fetch_undigested_matches() (match_category in ('strong','mixed'),
digested_at is null, and not yet applied to or dismissed -- no point
digesting something already acted on) and marks everything it sent via
mark_digested() so nothing goes out twice. One Slack message per run, not
one per posting -- if the send fails, nothing gets marked digested, so a
failed run is retried whole on the next run rather than needing to figure
out which postings partially went out.

NOTIFY_ON_EMPTY (below) sends a short "no new matches" message even when
there's nothing to digest -- temporary, for confirming the new cron
schedule is actually firing (see CLAUDE.md's "Scheduling and triggering").
Flip to False once that's trusted, to go back to fully silent on an empty
run.

Every digest also reports how many Claude API calls (discovery/categorize,
since the previous digest) failed outright -- rate limits, an
out-of-credits account, a hit max_budget_usd cap, auth issues. This is
deliberately separate from whether postings got processed: fetchers.py's
tiered capture degrades gracefully around individual call failures, so a
run can look completely fine ("N new matches!") while calls are actually
failing underneath -- this is the signal for that, not a substitute for
checking agent_runs directly when it's nonzero.
"""

import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests
from dotenv import load_dotenv

from agents.common import AgentRunTracker
from db.db import fetch_last_agent_run, fetch_undigested_matches, mark_digested, sum_llm_errors_since

load_dotenv(override=True)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")
SLACK_TIMEOUT_S = 15

# Temporary -- see module docstring. Flip to False to stop sending
# anything on a run with zero new matches.
NOTIFY_ON_EMPTY = True

MATCH_BADGE = {"strong": ":large_green_circle: strong", "mixed": ":large_yellow_circle: mixed"}


def _format_posting(posting) -> str:
    badge = MATCH_BADGE.get(posting.match_category, posting.match_category)
    lines = [f"*<{posting.url}|{posting.title}>* at *{posting.company}* -- {badge}"]
    meta = [posting.location or "location unknown"]
    if posting.work_location:
        meta.append(posting.work_location)
    if posting.salary_min:
        salary = f"${posting.salary_min:,.0f}-${posting.salary_max:,.0f}"
        if posting.salary_is_predicted:
            salary += " (estimated)"
        meta.append(salary)
    lines.append(" · ".join(str(m) for m in meta))
    if posting.match_notes:
        lines.append(posting.match_notes)
    return "\n".join(lines)


def _send_slack(text: str) -> None:
    response = requests.post(SLACK_WEBHOOK_URL, json={"text": text}, timeout=SLACK_TIMEOUT_S)
    response.raise_for_status()


def _llm_error_line() -> str:
    """How many discovery/categorize Claude calls failed outright since
    the last digest -- falls back to a 24h lookback on the very first
    digest ever, when there's no prior digest run to anchor "since" to."""
    last_digest = fetch_last_agent_run("digest")
    since = last_digest.finished_at if last_digest else datetime.now(timezone.utc) - timedelta(hours=24)
    count = sum_llm_errors_since(since)
    if count:
        return f"\n\n:warning: {count} LLM call error(s) since last digest -- check agent_runs/Langfuse."
    return "\n\n0 LLM call errors since last digest."


async def main() -> None:
    async with AgentRunTracker("digest") as run:
        postings = fetch_undigested_matches()
        logger.info("%d new match(es) to digest", len(postings))
        if not postings:
            if NOTIFY_ON_EMPTY:
                text = "No new job matches this run." + _llm_error_line()
                if SLACK_WEBHOOK_URL:
                    _send_slack(text)
                    logger.info("Sent empty-run confirmation to Slack")
                else:
                    logger.info("SLACK_WEBHOOK_URL not set -- printing digest to stdout instead")
                    print(text)
            return

        header = f"*{len(postings)} new job match(es)*"
        body = "\n\n".join(_format_posting(p) for p in postings)
        text = f"{header}\n\n{body}" + _llm_error_line()

        if SLACK_WEBHOOK_URL:
            _send_slack(text)
            logger.info("Sent digest to Slack (%d posting(s))", len(postings))
        else:
            logger.info("SLACK_WEBHOOK_URL not set -- printing digest to stdout instead")
            print(text)

        mark_digested([p.id for p in postings])
        for _ in postings:
            run.record(is_new=True)


if __name__ == "__main__":
    asyncio.run(main())
