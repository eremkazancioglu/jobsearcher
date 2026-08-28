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
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests
from dotenv import load_dotenv

from agents.common import AgentRunTracker
from db.db import fetch_undigested_matches, mark_digested

load_dotenv(override=True)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")
SLACK_TIMEOUT_S = 15

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


async def main() -> None:
    async with AgentRunTracker("digest") as run:
        postings = fetch_undigested_matches()
        logger.info("%d new match(es) to digest", len(postings))
        if not postings:
            return

        header = f"*{len(postings)} new job match(es)*"
        body = "\n\n".join(_format_posting(p) for p in postings)
        text = f"{header}\n\n{body}"

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
