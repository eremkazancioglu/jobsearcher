"""Whole-web search (LinkedIn excluded) for agents/digest_source.py, via the
Brave Search API.

Why Brave and not Claude's web_search tool (what this used before)? Cost,
once the rest of the pipeline got cheap: a Claude web_search call measured
about $0.027 per query (the flat $0.01 search fee plus ~14k input tokens of
results fed back to the model), while Brave charges a flat ~$0.005 per query
and no tokens at all. Quality was checked head-to-head earlier, before this
switch, on 5 real digest listings with only linkedin.com excluded: both
matched 5 of 5, with similar candidate depth, and 4 of 5 final matches
landed on the exact same URL. The search step here is mechanical retrieval
(a ranked list of URLs), so there's no reason to pay a model to do it.

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

Domain restriction is a blocklist, not an allowlist -- confirmed as the
right call. An allowlist of known ATS/job-board domains matched 2 of 5 real
listings, missing two (PwC, General Motors) hosted on the company's own
custom-branded careers portal -- no allowlist could anticipate every
company's own domain. Whole-web with only linkedin.com excluded matched 5
of 5. linkedin.com is the one domain excluded -- confirmed bot-blocked
(fetching a LinkedIn job page hits real bot-detection), so a candidate
there would just fail the fetch regardless of ranking. wellfound.com and
builtin.com are deliberately left unblocked despite being two of the three
digest sources -- confirmed not bot-blocked, full descriptions visible, no
login wall, and landing back on the exact listing there is often the most
accurate candidate available.

Brave has no blocked-domains parameter (unlike Claude's web_search), so the
exclusion is a `-site:` operator appended to the query text.
"""

import asyncio
import logging
import os
import time

import requests
from langfuse import observe

logger = logging.getLogger(__name__)

BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
BRAVE_TIMEOUT_S = 30
MAX_RESULTS = 10  # matches fetchers.py's MAX_FALLBACK_CANDIDATES
# Brave's Search plan list price ($5 per 1,000 queries). A flat per-query
# fee, so cost is a count x price, not derived from a usage block.
PRICE_PER_SEARCH_USD = 5.0 / 1_000

# Retry once on a 429 (rate limited) or 5xx, then give up -- same posture as
# the other retry helpers in this project: one retry, not unbounded.
RETRY_STATUSES = {429, 500, 502, 503, 504}
RETRY_BACKOFF_S = 2.0

# The one domain excluded from search -- see this module's docstring.
BLOCKED_DOMAINS = ["linkedin.com"]

_total_cost_usd = 0.0
_llm_error_count = 0


class SearchError(RuntimeError):
    """The search call itself failed (outage, bad/revoked key, exhausted
    quota) -- distinct from a search that worked and found nothing. Raised
    rather than returned as an empty list so digest_source records it as a
    per-listing error instead of a "no match", which would otherwise write
    a search-miss cooldown row and lock the listing out for days over a
    problem that had nothing to do with it."""


def get_total_cost_usd() -> float:
    """Cumulative USD cost of every search call in this process -- tracked
    separately from fetchers.py's counter (separate modules, each with its
    own in-process tally). Callers that want a whole-run total (e.g.
    digest_source.py) sum this with fetchers.get_total_cost_usd() themselves."""
    return _total_cost_usd


def get_llm_error_count() -> int:
    """Failed search calls. Named for the 'LLM errors' digest line it feeds
    (agent_runs.llm_errors) -- a Brave outage or a revoked key should
    surface in the same place a Claude failure does, since either one
    silently degrades results."""
    return _llm_error_count


def _build_query(query: str) -> str:
    exclusions = " ".join(f"-site:{domain}" for domain in BLOCKED_DOMAINS)
    return f"{query} {exclusions}"


def _search_sync(query: str) -> list[str]:
    """One Brave web search, retrying once on a transient failure. Raises
    requests.RequestException on a final failure; the async wrapper turns
    that into an empty result."""
    api_key = os.environ["BRAVE_API_KEY"].strip()
    params = {"q": _build_query(query), "count": MAX_RESULTS}
    headers = {"Accept": "application/json", "X-Subscription-Token": api_key}

    response = None
    for attempt in (1, 2):
        response = requests.get(
            BRAVE_SEARCH_URL, params=params, headers=headers, timeout=BRAVE_TIMEOUT_S
        )
        if response.status_code not in RETRY_STATUSES or attempt == 2:
            break
        logger.warning(
            "Brave search returned %d for %r, retrying once", response.status_code, query
        )
        time.sleep(RETRY_BACKOFF_S)

    response.raise_for_status()
    results = response.json().get("web", {}).get("results", [])
    return [r["url"] for r in results if r.get("url")]


@observe(name="digest_web_search")
async def search(query: str) -> list[str]:
    """One whole-web Brave search, excluding only BLOCKED_DOMAINS, returning
    up to MAX_RESULTS ranked candidate URLs in the order returned. Raises
    SearchError on an API-level failure (see SearchError); an empty list
    means the search worked and found nothing."""
    global _total_cost_usd, _llm_error_count
    try:
        urls = await asyncio.to_thread(_search_sync, query)
    except requests.RequestException as exc:
        _llm_error_count += 1
        # Report the exception type and status only, not the exception
        # object: a requests error carries the full request URL, and
        # there's no reason to risk echoing request details into public CI
        # logs (hence `from None` below too).
        status = getattr(getattr(exc, "response", None), "status_code", None)
        raise SearchError(
            f"Brave search failed for {query!r} ({type(exc).__name__}, status={status})"
        ) from None
    _total_cost_usd += PRICE_PER_SEARCH_USD
    return urls[:MAX_RESULTS]
