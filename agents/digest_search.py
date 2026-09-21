"""Domain-restricted web search for agents/digest_source.py, via the raw
Anthropic SDK's web_search tool -- a second deliberate raw-SDK exception in
this project, alongside categorize.py's.

Why not claude_agent_sdk's WebSearch (what fetchers.py's tier 3 uses for
Adzuna)? Domain restriction (allowed_domains) only exists on the raw
Messages API's web_search tool definition -- confirmed directly against
current Anthropic docs, not assumed -- and claude_agent_sdk's
ClaudeAgentOptions has no equivalent field (checked its dataclass fields
directly: no allowed_domains/blocked_domains anywhere). This module exists
specifically to reach that field.

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

This turned out better than the Google CSE plan it replaced: allowed_domains
covers subdomains automatically (a bare "greenhouse.io" entry matches
"boards.greenhouse.io", any "{company}.greenhouse.io", etc. -- confirmed
against current docs), so JOB_BOARD_DOMAINS below is bare hostnames, not
the "*.example.com" wildcards Google's Programmable Search Engine needed
(that pattern is explicitly invalid here). There's also no 50-domain cap
here the way there was on a newly created Programmable Search Engine.
"""

import logging

import anthropic
from langfuse import observe

# Import side effect: instruments this module's raw Anthropic SDK client
# for Langfuse tracing (AnthropicInstrumentor) -- same as categorize.py.
import observability.tracing  # noqa: F401

logger = logging.getLogger(__name__)

CLAUDE_MODEL = "claude-haiku-4-5-20251001"
CLAUDE_MAX_TOKENS = 1024
CLAUDE_QUERY_TIMEOUT_S = 30
MAX_RESULTS = 10  # matches fetchers.py's MAX_FALLBACK_CANDIDATES

# Haiku 4.5 pricing (per token) -- same constants and same reasoning as
# categorize.py: the raw SDK doesn't return a pre-computed total_cost_usd
# the way claude_agent_sdk's ResultMessage does.
_PRICE_INPUT = 1.0 / 1_000_000
_PRICE_CACHE_WRITE_5M = 1.25 / 1_000_000
_PRICE_CACHE_READ = 0.10 / 1_000_000
_PRICE_OUTPUT = 5.0 / 1_000_000

_client = anthropic.AsyncAnthropic(timeout=CLAUDE_QUERY_TIMEOUT_S)

# Single source of truth for the domain restriction actually in effect.
# wellfound.com and builtin.com are deliberately included (confirmed not
# bot-blocked, full descriptions visible with no login wall);
# linkedin.com is deliberately excluded (confirmed bot-blocked) -- see
# CLAUDE.md's Phase 4 section for what was checked before relying on any
# of this.
JOB_BOARD_DOMAINS = [
    "greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com",
    "smartrecruiters.com", "icims.com", "workable.com", "jobvite.com",
    "taleo.net", "successfactors.com", "adp.com", "applytojob.com",
    "jazzhr.com", "breezy.hr", "recruitee.com", "comeet.co",
    "teamtailor.com", "personio.com", "personio.de", "bamboohr.com",
    "dayforcehcm.com", "trakstar.com", "hiringthing.com", "jobscore.com",
    "avature.net", "eightfold.ai", "oraclecloud.com", "csod.com",
    "ultipro.com", "phenompeople.com", "paylocity.com", "zohorecruit.com",
    "freshteam.com", "homerun.co", "dice.com", "workatastartup.com",
    "otta.com", "himalayas.app", "remoteok.com", "remotive.com",
    "weworkremotely.com", "welcometothejungle.com", "wellfound.com", "builtin.com",
    # Lower confidence -- exact hostname not verified against a real posting yet:
    "paycomonline.net", "bullhornstaffing.com", "clearcompany.com", "jobadder.com",
]

_total_cost_usd = 0.0
_llm_error_count = 0


def _record_cost(usage) -> None:
    global _total_cost_usd
    _total_cost_usd += (
        (usage.input_tokens or 0) * _PRICE_INPUT
        + (getattr(usage, "cache_creation_input_tokens", None) or 0) * _PRICE_CACHE_WRITE_5M
        + (getattr(usage, "cache_read_input_tokens", None) or 0) * _PRICE_CACHE_READ
        + (usage.output_tokens or 0) * _PRICE_OUTPUT
    )


def get_total_cost_usd() -> float:
    """Cumulative USD cost of every search call in this process --
    tracked separately from fetchers.py's counter since this is a
    different Claude client (raw Anthropic SDK vs. claude_agent_sdk),
    same reasoning as categorize.py's independent counter. Callers that
    want a whole-run total (e.g. digest_source.py) sum this with
    fetchers.get_total_cost_usd() themselves."""
    return _total_cost_usd


def get_llm_error_count() -> int:
    return _llm_error_count


@observe(name="digest_web_search")
async def search(query: str) -> list[str]:
    """One web_search call, domain-restricted to JOB_BOARD_DOMAINS,
    returning up to MAX_RESULTS ranked candidate URLs in the order
    returned. Unlike fetchers.py's _web_search() (which asks Claude to
    report URLs back as structured output, since claude_agent_sdk's
    WebSearch result isn't otherwise inspectable), this reads the
    web_search_tool_result content blocks directly off the response --
    the raw Messages API already returns them structured, no second call
    needed. Returns an empty list (not raising) on an API-level failure,
    same graceful-degrade contract as fetchers.py's _web_search()."""
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
                    "allowed_domains": JOB_BOARD_DOMAINS,
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
