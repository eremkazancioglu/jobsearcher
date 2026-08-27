"""Tier 1/2/3 full-posting capture. Tier 1/2 is deterministic (no LLM call);
tier 3 uses LLM confirm/extract judgment calls.

Given an Adzuna search result (a title, company, location, description
snippet, and a redirect_url), try to recover the full job posting:

    Tier 1 -- plain fetch of redirect_url.
    Tier 2 -- headless render of redirect_url, if tier 1's text is too short.
    Tier 3 -- a single general web search for "{title} {company}", then the
              same tiered fetch walked over each result URL in order, until
              one passes confirmation.

Tier 1/2 needs no LLM judgment: redirect_url (after normalization) is
Adzuna's own /details/{id} page, which embeds a JobPosting JSON-LD block
with a clean description -- its presence is used directly, its absence is
itself the "not a valid live posting" signal (confirmed more reliable in
practice than judging visible text). Tier 3 candidates are genuinely
uncertain both in identity and in structure (arbitrary external sites), so
each one is judged by an LLM for (a) whether it's actually showing content
(not a login/paywall) and (b) whether it's the same posting Adzuna
described, before its text is trusted for extraction. If nothing works,
capture() degrades gracefully and keeps Adzuna's snippet -- see CLAUDE.md's
"Full posting capture" section for the full rationale.

This flow is kept independent of the Adzuna call that produced the result:
it's handed plain facts (company, title, location, snippet) and
independently looks for a public posting, never touching Adzuna's
redirect_url as anything other than "a URL to fetch."
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional

import requests
import trafilatura
from bs4 import BeautifulSoup
from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query
from playwright.async_api import async_playwright

from models.schema import AdzunaResult, DescriptionSource

logger = logging.getLogger(__name__)

MIN_USABLE_CHARS = 500
FETCH_TIMEOUT_S = 15
RENDER_TIMEOUT_MS = 20_000
# How many tier 3 search-result URLs to walk (in order) before giving up --
# each candidate costs a fetch (+ maybe a render) and, if usable-length, a
# confirm call, so this is a cost/thoroughness tradeoff, not just a
# thoroughness one. Confirmed in practice that the WebSearch tool's ranking
# can differ meaningfully from a plain Google search -- the actual company
# posting has landed as low as position 8 of 9 results for an exact
# "{title} {company}" query that ranked it #1 on Google. 10 effectively
# walks everything the tool tends to return rather than cutting off early.
MAX_FALLBACK_CANDIDATES = 10

# Confirm/extract/search are all narrow judgment/lookup tasks, not deep
# reasoning -- Haiku is plenty, and vastly cheaper than the CLI's default
# model.
CLAUDE_MODEL = "claude-haiku-4-5-20251001"
# Hard per-call ceiling so one runaway call can't blow through real money.
CLAUDE_MAX_BUDGET_USD = 0.15
CLAUDE_QUERY_TIMEOUT_S = 120
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

CONFIRM_AND_EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "gated": {
            "type": "boolean",
            "description": (
                "true if this page is a login wall, paywall, bot-block, or "
                "otherwise doesn't actually show posting content"
            ),
        },
        "same_posting": {
            "type": "boolean",
            "description": (
                "true only if this page describes the same specific job "
                "posting as the reference title/description/location -- "
                "not just the company's careers page in general"
            ),
        },
        "reason": {"type": "string"},
        "full_description": {
            "type": "string",
            "description": (
                "the full job description text, extracted from the page. "
                "Empty string if gated or not the same posting."
            ),
        },
        "salary_found": {
            "type": "boolean",
            "description": (
                "true only if the posting text itself states a salary or "
                "range. False if gated or not the same posting."
            ),
        },
        "salary_min": {"type": ["number", "null"]},
        "salary_max": {
            "type": ["number", "null"],
            "description": "if the posting states a single figure, set this equal to salary_min",
        },
        "work_location": {
            "type": "string",
            "enum": ["remote", "hybrid", "onsite", "unknown"],
            "description": (
                "remote, hybrid, or onsite if the JD states it; unknown if "
                "it doesn't say either way"
            ),
        },
    },
    "required": [
        "gated", "same_posting", "reason",
        "full_description", "salary_found", "salary_min", "salary_max", "work_location",
    ],
    "additionalProperties": False,
}

WORK_LOCATION_SCHEMA = {
    "type": "object",
    "properties": {
        "work_location": {
            "type": "string",
            "enum": ["remote", "hybrid", "onsite", "unknown"],
            "description": (
                "remote, hybrid, or onsite if the JD states it; unknown if "
                "it doesn't say either way"
            ),
        },
    },
    "required": ["work_location"],
    "additionalProperties": False,
}

SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "urls": {
            "type": "array",
            "items": {"type": "string"},
            "description": "result URLs from the search, in ranking order",
        },
    },
    "required": ["urls"],
    "additionalProperties": False,
}


@dataclass
class CaptureResult:
    description: str
    description_source: DescriptionSource
    url: str
    salary_min: Optional[Decimal]
    salary_max: Optional[Decimal]
    salary_is_predicted: Optional[bool]
    work_location: Optional[str]  # "remote" | "hybrid" | "onsite" | None


@dataclass
class PageFetch:
    text: str
    # The JobPosting JSON-LD description alone, unmerged with visible text
    # -- used directly as tier 1/2's deterministic full_description when
    # present, since it's already the site's own clean, isolated JD.
    metadata_text: str
    # True if Adzuna's own REMOTE badge was found in the raw HTML; None if
    # not found (not False -- absence isn't a "confirmed onsite" signal).
    remote_badge: Optional[bool]


_total_cost_usd = 0.0


def _record_cost(message: ResultMessage) -> None:
    global _total_cost_usd
    if message.total_cost_usd:
        _total_cost_usd += message.total_cost_usd


def get_total_cost_usd() -> float:
    """Cumulative USD cost of every Claude call made in this process so
    far (confirm/extract/tier 3 search), across all postings -- callers
    (e.g. discovery.py) log this at the end of a run."""
    return _total_cost_usd


def reset_total_cost_usd() -> None:
    global _total_cost_usd
    _total_cost_usd = 0.0


# Known-noise line the CLI prints on every subprocess launch when the
# machine has a claude.ai OAuth login alongside our ANTHROPIC_API_KEY --
# accurate but irrelevant here, since these calls always use the API key.
_NOISY_STDERR_SUBSTRINGS = ("claude.ai connectors are disabled",)


def _handle_claude_cli_stderr(line: str) -> None:
    if any(s in line for s in _NOISY_STDERR_SUBSTRINGS):
        return
    logger.debug("claude CLI stderr: %s", line)


async def _run_claude_json(
    prompt: str,
    schema: dict,
    *,
    allowed_tools: Optional[list[str]] = None,
    permission_mode: Optional[str] = None,
    max_turns: int = 1,
) -> Optional[dict]:
    options = ClaudeAgentOptions(
        output_format={"type": "json_schema", "schema": schema},
        allowed_tools=allowed_tools or [],
        permission_mode=permission_mode,
        max_turns=max_turns,
        model=CLAUDE_MODEL,
        max_budget_usd=CLAUDE_MAX_BUDGET_USD,
        # SDK isolation mode: these are narrow, stateless judgment calls,
        # not an interactive session -- they shouldn't depend on (or be
        # affected by) the user's global ~/.claude/settings.json or a
        # project CLAUDE.md.
        setting_sources=[],
        # The CLI's "claude.ai connectors are disabled..." notice is
        # printed directly to the subprocess's stderr, not through Python
        # logging -- setting_sources doesn't touch it (confirmed: still
        # printed with setting_sources=[]), since it's about detected
        # account-level OAuth state, not settings files. Route stderr
        # through our own logger instead of letting it print straight to
        # the terminal, filtering out just this one known-noise line.
        stderr=_handle_claude_cli_stderr,
    )

    async def _run() -> Optional[dict]:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, ResultMessage):
                _record_cost(message)
                if message.is_error:
                    # Demoted from warning: this is an expected, handled
                    # outcome (budget cap hit, timeout-ish failure, etc.)
                    # that every call site already logs a more specific
                    # follow-up for (e.g. "Confirm+extract call failed for
                    # %s") -- not something needing separate attention here.
                    logger.info("Claude query returned an error: %s", message.result)
                    return None
                return message.structured_output
        return None

    try:
        return await asyncio.wait_for(_run(), timeout=CLAUDE_QUERY_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.warning("Claude query timed out after %ss", CLAUDE_QUERY_TIMEOUT_S)
        return None
    except Exception:
        logger.exception("Claude query failed")
        return None


def _fetch_plain(url: str) -> Optional[PageFetch]:
    try:
        response = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=FETCH_TIMEOUT_S,
        )
        response.raise_for_status()
    except requests.RequestException as e:
        logger.info("Tier 1 plain fetch failed for %s: %s", url, e)
        return None
    page = _extract_text(response.text)
    logger.info(
        "Tier 1 plain fetch: %d chars (status %d) from %s",
        len(page.text), response.status_code, url,
    )
    return page


async def _fetch_rendered(url: str) -> Optional[PageFetch]:
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                pw_page = await browser.new_page(user_agent=USER_AGENT)
                await pw_page.goto(
                    url, wait_until="networkidle", timeout=RENDER_TIMEOUT_MS
                )
                html = await pw_page.content()
            finally:
                await browser.close()
    except Exception as e:
        logger.info("Tier 2 headless render failed for %s: %s", url, e)
        return None
    page = _extract_text(html)
    logger.info("Tier 2 headless render: %d chars from %s", len(page.text), url)
    return page


ADZUNA_LAND_AD_RE = re.compile(r"^(https?://www\.adzuna\.com)/land/ad/(\d+)")


def _normalize_redirect_url(url: str) -> str:
    """Adzuna's own /land/ad/{id} landing page has bot-protection that
    blocks both plain fetch (403) and headless render ("Access Denied") --
    confirmed in practice, including on postings where tier 1/2 would
    otherwise have to fall through to tier 3's full search. Its /details/{id}
    page serves the same JD directly with no such gate -- also confirmed in
    practice, on multiple postings, using the exact query string Adzuna's
    own API returned (not a separately-generated one). Swap to it when the
    pattern matches; any other URL (including every tier 3 candidate, which
    is never adzuna.com) passes through unchanged."""
    return ADZUNA_LAND_AD_RE.sub(r"\1/details/\2", url, count=1)


async def _fetch_tiered(url: str) -> Optional[PageFetch]:
    """Tier 1 (plain fetch), escalating to tier 2 (headless render) if the
    result looks too thin. Used for both the primary redirect_url and each
    tier 3 search-result candidate -- one fetch pipeline, not two."""
    page = _fetch_plain(url)
    if not _is_usable_length(page.text if page else None):
        logger.info(
            "Tier 1 text too short (%d/%d chars) for %s -- escalating to tier 2",
            len(page.text) if page else 0, MIN_USABLE_CHARS, url,
        )
        page = await _fetch_rendered(url)
    return page


def _iter_jsonld_nodes(data: Any):
    if isinstance(data, dict):
        graph = data.get("@graph")
        if isinstance(graph, list):
            yield from (node for node in graph if isinstance(node, dict))
        else:
            yield data
    elif isinstance(data, list):
        yield from (node for node in data if isinstance(node, dict))


def _is_job_posting_node(node: dict) -> bool:
    node_type = node.get("@type")
    if isinstance(node_type, list):
        return any(str(t).lower() == "jobposting" for t in node_type)
    return str(node_type).lower() == "jobposting"


def _extract_metadata_text(soup: BeautifulSoup) -> str:
    """Pull job-description text out of page *metadata*, not just what's
    visibly rendered. Some career sites (Workday-hosted ones, confirmed in
    practice) embed the full JD in a JobPosting JSON-LD block server-side,
    even though the visible page is a JS-rendered shell until the client
    app loads -- that content would otherwise be thrown away entirely.

    This is now load-bearing, not just a nice-to-have fallback: Adzuna's
    own /details/{id} page (tier 1/2's primary URL, after normalization)
    also embeds a JobPosting block this way, and capture() uses its
    presence/absence as the deterministic gate for the entire tier 1/2
    path -- see capture()'s tier 1/2 comment and CLAUDE.md's "Full posting
    capture" for why that replaced an LLM judgment call there."""
    parts = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        if not script.string:
            continue
        try:
            data = json.loads(script.string)
        except (json.JSONDecodeError, TypeError):
            continue
        for node in _iter_jsonld_nodes(data):
            if not _is_job_posting_node(node):
                continue
            description = node.get("description")
            if description:
                parts.append(BeautifulSoup(str(description), "html.parser").get_text("\n"))
    return "\n\n".join(parts)


def _extract_visible_text(html: str, soup: BeautifulSoup) -> str:
    """Main-content extraction, not "everything that isn't script/style" --
    trafilatura drops nav/menus/cookie banners/footers/"related jobs"
    widgets using general content-density heuristics (not site-specific
    rules), which cuts what actually gets sent to the LLM. Confirmed in
    practice: ~30% lower cost on a real posting, comparing this against the
    old whole-page-text approach on identical input. Falls back to that old
    approach if trafilatura finds no extractable "main content" at all
    (e.g. a near-empty JS-shell page) rather than silently returning
    nothing."""
    extracted = trafilatura.extract(html)
    if extracted:
        return extracted
    lines = (line.strip() for line in soup.get_text("\n").splitlines())
    return "\n".join(line for line in lines if line)


def _detect_remote_badge(soup: BeautifulSoup) -> Optional[bool]:
    """Deterministic remote-vs-not signal from Adzuna's own location-type
    badge -- a short span/div whose entire displayed text is exactly
    "REMOTE" -- extracted from raw HTML before cleaning strips it
    (confirmed in practice: trafilatura discards this element, same as the
    salary widget). Anchored on the badge's literal text, not its Tailwind
    CSS classes, which are purely cosmetic and cheap for Adzuna to change
    without changing what the badge actually says. Returns True if found,
    None otherwise -- absence isn't a "confirmed onsite" signal, just "no
    badge here"; capture() falls back to the LLM's own read of the JD text
    in that case. No confirmed HYBRID badge exists to match against, so
    this only ever returns True or None, never False."""
    for tag in soup.find_all(["span", "div"]):
        if tag.find(True) is not None:
            continue  # only leaf-ish elements -- a badge, not a wrapping container
        if tag.get_text(strip=True).upper() == "REMOTE":
            return True
    return None


def _extract_text(html: str) -> PageFetch:
    soup = BeautifulSoup(html, "html.parser")
    metadata_text = _extract_metadata_text(soup)
    remote_badge = _detect_remote_badge(soup)
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    visible_text = _extract_visible_text(html, soup)
    text = f"{metadata_text}\n\n{visible_text}" if metadata_text and visible_text else (metadata_text or visible_text)
    return PageFetch(text=text, metadata_text=metadata_text, remote_badge=remote_badge)


def _is_usable_length(text: Optional[str]) -> bool:
    return bool(text) and len(text) >= MIN_USABLE_CHARS


async def _confirm_and_extract(text: str, adzuna: AdzunaResult) -> Optional[dict]:
    """Judge and extract in a single call, not two -- both operate on the
    same page text, so this halves the LLM round-trips per candidate versus
    a separate confirm-then-extract pass. If the page turns out to be gated
    or the wrong posting, the model is told to leave the extraction fields
    empty/false rather than guess."""
    prompt = (
        "Judge the page text below against this reference job posting, "
        "then extract from it if -- and only if -- it's a genuine match.\n\n"
        "First, judge whether the page is (a) actually showing content "
        "(not a login wall, paywall, or bot-block) and (b) describing the "
        "same specific job posting as the reference, not just a company's "
        "careers page in general.\n\n"
        f"Reference title: {adzuna.title}\n"
        f"Reference company: {adzuna.company}\n"
        f"Reference location: {adzuna.location or 'unknown'}\n"
        f"Reference description snippet: {adzuna.description or '(none)'}\n\n"
        "If the page is gated or not the same posting, set full_description "
        "to an empty string, salary_found to false, and work_location to "
        "unknown -- don't extract anything from the wrong page. Otherwise, "
        "extract the full job description; separately state whether the "
        "posting itself states a salary or salary range (do not infer or "
        "estimate one if it doesn't); and classify the work location "
        "arrangement as exactly one of: remote, hybrid, onsite, unknown "
        "(use unknown only if the JD genuinely doesn't state this either "
        "way).\n\n"
        f"Page text:\n{text[:18000]}"
    )
    return await _run_claude_json(prompt, CONFIRM_AND_EXTRACT_SCHEMA)


def _normalize_work_location(value: Any) -> Optional[str]:
    return value if value in ("remote", "hybrid", "onsite") else None


async def _classify_work_location(text: str) -> Optional[str]:
    """Minimal, output-bounded LLM call: classify remote/hybrid/onsite from
    JD text alone, nothing else. Output is a single enum word, so cost
    stays low even though it's a real LLM call -- unlike the rest of tier
    1/2, which makes none at all (see capture()'s tier 1/2 block). Only
    invoked when the deterministic REMOTE badge (_detect_remote_badge)
    didn't already answer this -- calling it unconditionally would mean
    paying for a judgment the badge already gave for free."""
    prompt = (
        "Read the job description below and classify its work location "
        "arrangement as exactly one of: remote, hybrid, onsite, unknown. "
        "Use unknown only if the JD genuinely doesn't state this either "
        "way -- don't guess.\n\n"
        f"Job description:\n{text[:8000]}"
    )
    result = await _run_claude_json(prompt, WORK_LOCATION_SCHEMA)
    return _normalize_work_location(result.get("work_location")) if result else None


async def _web_search(query_text: str) -> list[str]:
    """A single, plain web search -- the same kind of query a person would
    type in by hand. Mechanical retrieval, not a judgment call, so this is
    a single tool call with no browsing/fetching/evaluating: it reports the
    result URLs and stops. The judgment of whether any given result is
    actually the right posting is _confirm_and_extract()'s job, applied
    per-candidate in capture(), not duplicated here."""
    prompt = (
        "Run exactly one web search for the query below and report back the "
        "result URLs, in ranking order. Do not fetch, browse, open, or "
        "evaluate any of the pages -- just report the URLs the search "
        "results already give you.\n\n"
        f"Query: {query_text}"
    )
    result = await _run_claude_json(
        prompt,
        SEARCH_SCHEMA,
        allowed_tools=["WebSearch"],
        permission_mode="bypassPermissions",
        # WebSearch is a deferred tool in this harness -- the model must
        # call ToolSearch to fetch its schema before it can invoke it, which
        # eats a turn on top of the search call itself and the final
        # structured-output call. 3 left zero margin and silently truncated
        # mid-call; give it real headroom.
        max_turns=6,
    )
    if not result:
        logger.info("Web search call failed or returned nothing for %r", query_text)
        return []
    return [url for url in result.get("urls", []) if isinstance(url, str)]


async def _try_confirmed_extraction(text: Optional[str], adzuna: AdzunaResult) -> Optional[dict]:
    """Confirm identity and extract in one call. Used for tier 3 candidates
    only -- external pages (search results or a company's careers site)
    whose identity is genuinely uncertain, unlike tier 1/2's primary URL,
    which is handled deterministically in capture() (see there for why).
    Returns the result dict (usable directly as an extraction --
    full_description/salary_* fields) or None if the text was too short,
    the call failed, or the page was rejected as gated / not the same
    posting."""
    if not _is_usable_length(text):
        logger.info(
            "Skipping confirm+extract for %s -- text too short/missing (%d chars)",
            adzuna.title, len(text) if text else 0,
        )
        return None
    result = await _confirm_and_extract(text, adzuna)
    if result is None:
        logger.info("Confirm+extract call failed for %s", adzuna.title)
        return None
    if result["gated"]:
        logger.info(
            "Confirm+extract rejected candidate for %s: judged gated (%s)",
            adzuna.title, result.get("reason"),
        )
        return None
    if not result["same_posting"]:
        logger.info(
            "Confirm+extract rejected candidate for %s: not judged the same posting (%s)",
            adzuna.title, result.get("reason"),
        )
        return None
    logger.info("Confirm+extract passed for %s", adzuna.title)
    return result


async def capture(adzuna: AdzunaResult) -> CaptureResult:
    """Best-effort full posting capture. Always returns a result -- degrades
    to Adzuna's snippet rather than raising when nothing works."""

    extraction = None
    description_source: DescriptionSource = "adzuna_snippet"
    url = adzuna.redirect_url
    remote_badge: Optional[bool] = None
    normalized_url = _normalize_redirect_url(adzuna.redirect_url)
    if normalized_url != adzuna.redirect_url:
        logger.info(
            "Tier 1/2: normalized redirect_url for %s: %s -> %s",
            adzuna.title, adzuna.redirect_url, normalized_url,
        )

    logger.info("Tier 1/2: trying %s -> %s", adzuna.title, normalized_url)
    try:
        page = await _fetch_tiered(normalized_url)
        # Deterministic, not LLM-judged: Adzuna's own /details/{id} page
        # embeds a JobPosting JSON-LD block with a clean, isolated JD
        # description (_extract_metadata_text() already pulls this out as
        # page.metadata_text). Its presence is the validity gate -- no
        # separate "is this gated/the right posting" judgment call needed,
        # since this page is served directly by this exact posting's own
        # ID, not a third-party page that could show something else.
        # Confirmed in practice that absence is itself a reliable "this
        # isn't a valid live posting" signal: on a listing that expired
        # since it was first captured, the visible page still looked
        # plausible, but the JobPosting node was gone -- a more reliable
        # gate than judging visible text would have been.
        if page and _is_usable_length(page.metadata_text):
            extraction = {"full_description": page.metadata_text}
            description_source = "redirect_url"
            url = normalized_url
            remote_badge = page.remote_badge
            logger.info(
                "Tier 1/2: deterministic JD from JobPosting JSON-LD for %s (%d chars)",
                adzuna.title, len(page.metadata_text),
            )
        else:
            logger.info(
                "Tier 1/2: no usable JobPosting JSON-LD for %s -- falling through to tier 3",
                adzuna.title,
            )
    except Exception:
        logger.exception("Tier 1/2 capture failed for %s", normalized_url)

    if extraction is None:
        try:
            search_query = f"{adzuna.title} {adzuna.company}"
            candidate_urls = await _web_search(search_query)
            walked_urls = candidate_urls[:MAX_FALLBACK_CANDIDATES]
            logger.info(
                "Tier 3: searched %r for %s, got %d result(s), walking %d: %s",
                search_query, adzuna.title, len(candidate_urls), len(walked_urls),
                walked_urls,
            )
            if len(candidate_urls) > MAX_FALLBACK_CANDIDATES:
                logger.info(
                    "Tier 3: %d result(s) beyond the cap were not walked: %s",
                    len(candidate_urls) - MAX_FALLBACK_CANDIDATES,
                    candidate_urls[MAX_FALLBACK_CANDIDATES:],
                )
            for candidate_url in walked_urls:
                candidate_page = await _fetch_tiered(candidate_url)
                candidate_extraction = await _try_confirmed_extraction(
                    candidate_page.text if candidate_page else None, adzuna
                )
                if candidate_extraction is not None:
                    extraction = candidate_extraction
                    description_source = "company_site"
                    url = candidate_url
                    remote_badge = candidate_page.remote_badge if candidate_page else None
                    logger.info("Tier 3: candidate %s confirmed for %s", candidate_url, adzuna.title)
                    break
            else:
                logger.info("Tier 3: no candidate confirmed for %s", adzuna.title)
        except Exception:
            logger.exception("Tier 3 fallback search failed for %s", adzuna.title)

    # Salary detection only runs for tier 3 (_confirm_and_extract) -- tier
    # 1/2's deterministic extraction dict has no salary_* keys at all,
    # since that page IS Adzuna's own data source, not an independent one
    # to check against Adzuna's salary fields. .get() (not bracket access)
    # is deliberate here so a tier 1/2 result falls straight through to
    # Adzuna's own salary_min/salary_max/salary_is_predicted.
    salary_min, salary_max, salary_is_predicted = None, None, None
    if extraction is not None and extraction.get("salary_found"):
        try:
            salary_min = Decimal(str(extraction["salary_min"]))
            salary_max_value = extraction["salary_max"]
            salary_max = (
                Decimal(str(salary_max_value)) if salary_max_value is not None else salary_min
            )
            salary_is_predicted = False
        except (TypeError, ArithmeticError):
            logger.warning("Malformed salary in extraction for %s; falling back to Adzuna's", adzuna.title)
            salary_min = salary_max = salary_is_predicted = None
    if salary_min is None:
        salary_min = adzuna.salary_min
        salary_max = adzuna.salary_max
        salary_is_predicted = adzuna.salary_is_predicted

    description = (
        extraction["full_description"] if extraction is not None else adzuna.description
    ) or ""

    # Adzuna's own REMOTE badge (deterministic, when present) wins outright
    # -- it's a direct site-provided signal, not a guess. Otherwise: for
    # tier 1/2 (no LLM call anywhere else on that path), a minimal,
    # output-bounded classification call answers remote/hybrid/onsite from
    # the JD text alone -- a real, deliberate LLM cost added back
    # specifically for this field, kept small by asking for nothing but a
    # single enum word. For tier 3, work_location already came back as
    # part of the same confirm+extract call, so no extra call is needed.
    if remote_badge:
        work_location = "remote"
    elif description_source == "redirect_url" and extraction is not None:
        work_location = await _classify_work_location(extraction["full_description"])
    elif extraction is not None:
        work_location = _normalize_work_location(extraction.get("work_location"))
    else:
        work_location = None

    return CaptureResult(
        description=description,
        description_source=description_source,
        url=url,
        salary_min=salary_min,
        salary_max=salary_max,
        salary_is_predicted=salary_is_predicted,
        work_location=work_location,
    )
