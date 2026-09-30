"""Whole-web search (LinkedIn excluded) for agents/digest_source.py, via the
raw Anthropic SDK's web_search tool.

Why the raw SDK's web_search tool and not claude_agent_sdk's WebSearch?
Domain filtering (allowed_domains/blocked_domains) only exists on the raw
Messages API's web_search tool definition -- confirmed directly against
current Anthropic docs, not assumed -- and claude_agent_sdk's
ClaudeAgentOptions has no equivalent field (checked its dataclass fields
directly). claude_agent_sdk is no longer used anywhere in this project.

Why not Google's Custom Search JSON API (the original plan for this)?
Confirmed in practice, not a config mistake on our end: Google closed
Custom Search JSON API to new customers some time before this was built --
two separate from-scratch Google Cloud projects, correctly configured
(API enabled, billing linked, key correctly scoped), both failed
identically with "This project does not have the access to Custom Search
JSON API." Confirmed via Google's own developer/support forums that this
is a real, current policy (existing customers keep access until the
API's January 2027 discontinuation; new customers get nothing), not
fixable by any project configuration. See CLAUDE.md's Phase 4 section for
the full investigation.

Domain restriction here is a blocklist (blocked_domains=["linkedin.com"]),
not an allowlist -- confirmed as the right call, not the original design.
The original allowed_domains=JOB_BOARD_DOMAINS approach (an allowlist of
known ATS/job-board domains, inherited from the Google CSE plan it
replaced) was tested head-to-head against whole-web + blocklist on 5 real
digest listings: the allowlist matched 2 of 5, missing two postings
(PwC, General Motors) that turned out to be hosted on the company's own
custom-branded careers portal (jobs-us.pwc.com, search-careers.gm.com) --
neither on the allowlist, and no allowlist could ever anticipate every
company's own domain in advance. Whole-web + blocklist matched 5 of 5 on
the same listings, confirming this (Adzuna's former tier 3 search,
since removed, never used a domain allowlist either). linkedin.com is the one domain still excluded --
confirmed bot-blocked (fetching a LinkedIn job page hits real
bot-detection), so a candidate there would just fail the fetch regardless
of ranking. wellfound.com and builtin.com are deliberately left
unblocked despite being two of the three digest sources -- confirmed not
bot-blocked, full descriptions visible, no login wall, and landing back
on the exact listing there is often the most accurate candidate
available. See CLAUDE.md's Phase 4 section for the full comparison,
including the Brave Search API side-by-side that confirmed this isn't
about search-provider quality: Claude's web_search and Brave performed
near-identically once the allowlist was removed.
"""

import logging

import anthropic
from langfuse import observe

# Import side effect: instruments this module's raw Anthropic SDK client
# for Langfuse tracing (AnthropicInstrumentor) -- same as categorize.py.
import observability.tracing  # noqa: F401
from agents.pricing import usage_cost_usd

logger = logging.getLogger(__name__)

CLAUDE_MODEL = "claude-haiku-4-5-20251001"
CLAUDE_MAX_TOKENS = 1024
CLAUDE_QUERY_TIMEOUT_S = 30
MAX_RESULTS = 10  # matches fetchers.py's MAX_FALLBACK_CANDIDATES

_client = anthropic.AsyncAnthropic(timeout=CLAUDE_QUERY_TIMEOUT_S)

# The one domain excluded from search -- confirmed bot-blocked (fetching a
# LinkedIn job page hits real bot-detection), so a candidate there would
# just fail the fetch regardless of ranking. wellfound.com and builtin.com
# (the other two digest sources) are deliberately NOT here -- see this
# module's docstring for why.
BLOCKED_DOMAINS = ["linkedin.com"]

_total_cost_usd = 0.0
_llm_error_count = 0


def _record_cost(usage) -> None:
    global _total_cost_usd
    _total_cost_usd += usage_cost_usd(usage)


def get_total_cost_usd() -> float:
    """Cumulative USD cost of every search call in this process --
    tracked separately from fetchers.py's counter (separate modules, each
    with its own in-process tally). Includes the flat per-search web_search
    fee, not just tokens (see agents/pricing.py) -- before this was counted
    the logged cost understated each search by about 40%. Callers that want
    a whole-run total (e.g. digest_source.py) sum this with
    fetchers.get_total_cost_usd() themselves."""
    return _total_cost_usd


def get_llm_error_count() -> int:
    return _llm_error_count


@observe(name="digest_web_search")
async def search(query: str) -> list[str]:
    """One whole-web web_search call, excluding only BLOCKED_DOMAINS,
    returning up to MAX_RESULTS ranked candidate URLs in the order
    returned. Reads the web_search_tool_result content blocks directly off
    the response -- the raw Messages API already returns them structured,
    so no second call is needed to get the URLs out. Returns an empty list
    (not raising) on an API-level failure, so a failed search degrades to
    "no candidates" rather than crashing the run."""
    global _llm_error_count
    try:
        response = await _client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=CLAUDE_MAX_TOKENS,
            messages=[{"role": "user", "content": f"Search the web for: {query}"}],
            tools=[
                {
                    "type": "web_search_20250305",
                    "name": "web_search",
                    "max_uses": 1,
                    "blocked_domains": BLOCKED_DOMAINS,
                }
            ],
        )
    except anthropic.APIError:
        _llm_error_count += 1
        logger.exception("Web search call failed for %r", query)
        return []
    _record_cost(response.usage)

    urls = []
    for block in response.content:
        if getattr(block, "type", None) != "web_search_tool_result":
            continue
        content = block.content
        if not isinstance(content, list):
            continue  # a web_search_tool_result_error, not a result list
        for result in content:
            if getattr(result, "type", None) == "web_search_result":
                urls.append(result.url)

    return urls[:MAX_RESULTS]
