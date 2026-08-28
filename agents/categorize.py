"""Categorization agent: judges strong/mixed/weak fit for every posting
where match_category is still null, against both the resume (what the
person is qualified for) and the preferences document (what they actually
want) -- see CLAUDE.md's "Fit categorization" for why both are used, not
just the resume.

Run manually while Phase 2 is validated, same as discovery.py in Phase 1:

    uv run agents/categorize.py

Add --limit to cap how many postings a single run judges (useful for a
smoke test before running against everything):

    uv run agents/categorize.py --limit 5

Uses the raw Anthropic SDK, not claude_agent_sdk (every other Claude call
in this project uses claude_agent_sdk -- this is the deliberate exception).
Measured directly on a real call: claude_agent_sdk's CLI harness adds
~18,600 tokens of its own system prompt + built-in tool declarations to
every call (billed as a 1.25x cache write each time, since it's never
reused across separate query() invocations -- see fetchers.py's "Cost
reality check"), on top of our own ~2,800 tokens of actual content
(resume+preferences+JD). That overhead accounted for ~73% of a call's
cost. Prompt caching wasn't a usable fix for it even with the raw SDK --
Haiku 4.5 requires 4,096+ tokens to cache at all, and our own content
(~2,800 tokens) doesn't clear that either -- so the fix here is simply not
paying for a coding-agent harness this call never needed, not caching.
The same measurement also showed claude_agent_sdk defaulting to extended
thinking (1,348 of 1,511 output tokens on that call) for a narrow
classification task that doesn't need it; the raw SDK doesn't enable
thinking unless asked, so omitting the `thinking` param here is a second,
independent saving on top of the harness-overhead fix.
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import anthropic
from langfuse import observe

from agents.common import AgentRunTracker
from agents.preferences import load_preferences
from agents.resume import load_resume
from db.db import update_posting_category, fetch_uncategorized_postings
# Import side effect: initializes the Langfuse client and instruments the
# raw Anthropic SDK client below (AnthropicInstrumentor) -- see
# observability/tracing.py.
import observability.tracing  # noqa: F401

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)
# Quiet the SDK's own per-request INFO line ("HTTP Request: POST ... 200 OK")
# -- not useful at our log level, same reasoning as discovery.py silencing
# claude_agent_sdk's own noise.
logging.getLogger("httpx2").setLevel(logging.WARNING)

# Same reasoning as fetchers.py's narrow judgment calls for model choice:
# Haiku. Revisit if categorization quality against the full
# resume+preferences+JD context turns out to need a stronger model --
# unlike tier 3's same-posting/no judgment calls, this one is a genuinely
# holistic read, not a narrow lookup, so it's worth watching in practice
# rather than assuming Haiku is automatically sufficient here too.
CLAUDE_MODEL = "claude-haiku-4-5-20251001"
# Output is always a short enum + one sentence -- this bounds worst-case
# output cost directly, replacing claude_agent_sdk's max_budget_usd (which
# has no raw-SDK equivalent; not needed here since input is also bounded,
# by the 12000-char description cap below).
CLAUDE_MAX_TOKENS = 1024
CLAUDE_QUERY_TIMEOUT_S = 120

# Haiku 4.5 pricing (per token, not per million) -- used to compute actual
# spend from each response's usage block, same purpose as fetchers.py's
# get_total_cost_usd() but derived locally since the raw SDK doesn't return
# a pre-computed total_cost_usd the way claude_agent_sdk's ResultMessage does.
_PRICE_INPUT = 1.0 / 1_000_000
_PRICE_CACHE_WRITE_5M = 1.25 / 1_000_000
_PRICE_CACHE_WRITE_1H = 2.0 / 1_000_000
_PRICE_CACHE_READ = 0.10 / 1_000_000
_PRICE_OUTPUT = 5.0 / 1_000_000

_client = anthropic.AsyncAnthropic(timeout=CLAUDE_QUERY_TIMEOUT_S)

CATEGORIZE_SCHEMA = {
    "type": "object",
    "properties": {
        "match_category": {
            "type": "string",
            "enum": ["strong", "mixed", "weak"],
            "description": (
                "strong: qualified per the resume AND fits what the "
                "preferences document wants. mixed: qualified but a "
                "meaningful preference mismatch (or vice versa) -- still "
                "worth a human glance. weak: not qualified, or a clear "
                "preference deal-breaker (e.g. explicitly ruled-out "
                "location/industry/role type)."
            ),
        },
        "match_notes": {
            "type": "string",
            "description": "one sentence explaining the judgment -- cite the specific resume or preference reason.",
        },
    },
    "required": ["match_category", "match_notes"],
    "additionalProperties": False,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fit categorization against resume + preferences")
    parser.add_argument("--limit", type=int, default=None, help="Max postings to judge this run")
    return parser.parse_args()


_total_cost_usd = 0.0


def _record_cost(usage) -> None:
    global _total_cost_usd
    _total_cost_usd += (
        (usage.input_tokens or 0) * _PRICE_INPUT
        + (getattr(usage, "cache_creation_input_tokens", None) or 0) * _PRICE_CACHE_WRITE_5M
        + (getattr(usage, "cache_read_input_tokens", None) or 0) * _PRICE_CACHE_READ
        + (usage.output_tokens or 0) * _PRICE_OUTPUT
    )


def get_total_cost_usd() -> float:
    """Cumulative USD cost of every Claude call made in this process so
    far, across all postings -- same pattern as fetchers.py's cost
    tracking, logged by main() after every posting and as a final total."""
    return _total_cost_usd


_llm_error_count = 0


def get_llm_error_count() -> int:
    """Cumulative count of Claude API calls that raised outright this
    process -- rate limits, an out-of-credits account, auth failures,
    connection errors. Same purpose as fetchers.py's get_llm_error_count();
    tracked separately since these are two different Claude clients
    (raw Anthropic SDK here vs. claude_agent_sdk there)."""
    return _llm_error_count


def reset_llm_error_count() -> None:
    global _llm_error_count
    _llm_error_count = 0


@observe(name="categorize_posting")
async def _categorize(resume: str, preferences: str, posting) -> dict:
    prompt = (
        "Judge this job posting's fit for the candidate below, against "
        "BOTH documents -- the resume (what they're qualified for) and "
        "the preferences (what they actually want). A posting that's a "
        "great resume match but violates a clear preference is not a "
        "strong match; weigh both.\n\n"
        f"## Resume\n{resume}\n\n"
        f"## Preferences\n{preferences}\n\n"
        "## Job posting\n"
        f"Title: {posting.title}\n"
        f"Company: {posting.company}\n"
        f"Location: {posting.location or 'unknown'}\n"
        f"Work location: {posting.work_location or 'unknown'}\n"
        f"Salary: {posting.salary_min or 'unknown'}-{posting.salary_max or 'unknown'}"
        f"{' (estimated)' if posting.salary_is_predicted else ''}\n\n"
        f"Description:\n{(posting.description or '')[:12000]}"
    )
    global _llm_error_count
    try:
        response = await _client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=CLAUDE_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": CATEGORIZE_SCHEMA}},
        )
    except anthropic.APIError:
        # Specifically an API-level failure (rate limit, insufficient
        # credits, auth, connection) -- not a malformed-response issue
        # (handled below, not counted here). Re-raised so the existing
        # per-posting error handling in main() still applies; this just
        # also counts it before that happens.
        _llm_error_count += 1
        raise
    _record_cost(response.usage)

    text = next((b.text for b in response.content if b.type == "text"), None)
    if text is None:
        raise RuntimeError(f"Claude response had no text block (stop_reason={response.stop_reason})")
    return json.loads(text)


async def main() -> None:
    args = parse_args()
    resume = load_resume()
    preferences = load_preferences()

    postings = fetch_uncategorized_postings()
    if args.limit is not None:
        postings = postings[: args.limit]
    logger.info("%d posting(s) awaiting categorization", len(postings))

    async with AgentRunTracker("categorize") as run:
        for posting in postings:
            try:
                result = await _categorize(resume, preferences, posting)
                update_posting_category(posting.id, result["match_category"], result["match_notes"])
                logger.info(
                    "%s at %s -> %s (%s)",
                    posting.title, posting.company, result["match_category"], result["match_notes"],
                )
                run.record(is_new=True)
                logger.info("Running total Claude API cost so far: $%.4f", get_total_cost_usd())
            except Exception as e:
                run.record_error(f"{posting.id} ({posting.title}): {e}")
        run.llm_errors = get_llm_error_count()

    logger.info("Run complete. Total Claude API cost: $%.4f", get_total_cost_usd())


if __name__ == "__main__":
    asyncio.run(main())
