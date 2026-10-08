"""Full-posting capture: the Adzuna tier 1/2 path, plus the shared
candidate walk and confirm+extract judgment used by agents/digest_source.py.

capture() (Adzuna): given a search result (title, company, location,
snippet, redirect_url), recover the full posting with no LLM judgment:

    Tier 1 -- plain fetch of redirect_url.
    Tier 2 -- headless render of redirect_url, if tier 1's text is too short.

redirect_url (after normalization) is Adzuna's own /details/{id} page, which
embeds a JobPosting JSON-LD block with a clean description -- its presence
is used directly, its absence is itself the "not a valid live posting"
signal (confirmed more reliable in practice than judging visible text). If
that fails, capture() logs it and keeps Adzuna's snippet. There is no longer
a tier 3 (search + candidate walk) for Adzuna: tier 1/2 resolves nearly
every posting, so it almost never ran and was the most expensive path.

capture_from_search() / walk_candidates() (digest source): candidates come
from a caller's own search, each is fetched through the same tiered fetch
and judged by a confirm+extract LLM call for (a) whether it's actually
showing content (not a login/paywall) and (b) whether it's the same posting
as the reference, before its text is trusted. No LLM is involved unless a
candidate page has a usable amount of text.

All LLM calls here go through the raw Anthropic SDK (not claude_agent_sdk):
they're single-shot structured-output calls with no tools, and the agent
SDK's CLI harness added a ~20k-token system prompt + tool-definition cache
write to every one of them (about 70-85% of each call's cost, measured).
See CLAUDE.md's "Cost controls" sections.
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional

import anthropic
import requests
import trafilatura
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from agents.pricing import usage_cost_usd
from models.schema import AdzunaResult, DescriptionSource
from langfuse import observe

# Import side effect: instruments the raw Anthropic client below for Langfuse
# tracing (AnthropicInstrumentor) -- see observability/tracing.py.
import observability.tracing  # noqa: F401

logger = logging.getLogger(__name__)

MIN_USABLE_CHARS = 500
FETCH_TIMEOUT_S = 15
RENDER_TIMEOUT_MS = 20_000
# How many search-result URLs to walk (in order) before giving up --
# each candidate costs a fetch (+ maybe a render) and, if usable-length, a
# confirm call, so this is a cost/thoroughness tradeoff, not just a
# thoroughness one. Confirmed in practice that a search tool's ranking can
# differ meaningfully from a plain Google search -- the actual company
# posting has landed as low as position 8 of 9 results for an exact
# "{title} {company}" query that ranked it #1 on Google. 10 effectively
# walks everything the tool tends to return rather than cutting off early.
MAX_FALLBACK_CANDIDATES = 10

# Confirm/extract and work-location classification are narrow judgment
# tasks, not deep reasoning -- Haiku is plenty. Thinking is left off (the
# raw SDK doesn't enable it unless asked); validated against known
# accept/reject cases before switching, see CLAUDE.md.
CLAUDE_MODEL = "claude-haiku-5-5"
# Bounds worst-case output per call (the confirm+extract schema echoes the
# job description back, capped at ~18k input characters) -- replaces the
# per-call max_budget_usd cap claude_agent_sdk offered, which has no raw-SDK
# equivalent.
CLAUDE_MAX_TOKENS = 8192
CLAUDE_QUERY_TIMEOUT_S = 120
_client = anthropic.AsyncAnthropic(timeout=CLAUDE_QUERY_TIMEOUT_S)
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
class PostingReference:
    """The plain facts _confirm_and_extract() judges a candidate page
    against -- factored out of AdzunaResult so the candidate walk can be
    shared with sources that aren't Adzuna (e.g. agents/digest_source.py,
    which only ever has title/company, no snippet or location)."""
    title: str
    company: str
    location: Optional[str] = None
    description: Optional[str] = None


@dataclass
class CandidateMatch:
    url: str
    extraction: dict
    remote_badge: Optional[bool]


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
    # The page's real title -- the JobPosting JSON-LD node's own `title`
    # field when present, falling back to the raw HTML <title> tag
    # otherwise -- separate from the extracted body text. Confirmed
    # necessary to source it this way, not just from the raw <title> tag
    # alone: on a JS-shell page (Workday-hosted ones -- same case
    # _extract_metadata_text() exists for), the client sets the real page
    # title after hydration, so a plain fetch's <title> tag comes back
    # empty even though the same JSON-LD block supplying the JD text also
    # has the real role title in it. Passed to _confirm_and_extract() as
    # an extra deterministic signal specifically because relying on
    # title-string matching against body content that may not mention the
    # title at all (trafilatura's extraction strips page headings as
    # chrome, the same way it strips nav/footer) is what let a Staff IC
    # role get wrongly confirmed against a "Manager" reference title on a
    # real run -- see CLAUDE.md's Phase 4 section.
    page_title: Optional[str] = None


_total_cost_usd = 0.0


def _record_cost(usage) -> None:
    global _total_cost_usd
    _total_cost_usd += usage_cost_usd(usage, CLAUDE_MODEL)


def get_total_cost_usd() -> float:
    """Cumulative USD cost of every Claude call made in this process so
    far (confirm+extract, work-location classification), across all
    postings -- callers (e.g. discovery.py) log this at the end of a run.
    Computed locally from each response's usage block (agents/pricing.py),
    since the raw SDK doesn't return a pre-computed total."""
    return _total_cost_usd


def reset_total_cost_usd() -> None:
    global _total_cost_usd
    _total_cost_usd = 0.0


_llm_error_count = 0


def _record_llm_error() -> None:
    global _llm_error_count
    _llm_error_count += 1


def get_llm_error_count() -> int:
    """Cumulative count of Claude calls that failed outright this process
    -- anthropic.APIError (rate limit, out-of-credits, auth, connection,
    timeout) or an unparseable/truncated response. Distinct from a
    posting merely degrading to the Adzuna snippet or a candidate being
    legitimately rejected, which is normal, expected behavior, not an
    error -- this counts actual call failures specifically, so
    infrastructure problems (billing, credits, rate limits) are visible
    even when the pipeline otherwise degrades gracefully around them."""
    return _llm_error_count


def reset_llm_error_count() -> None:
    global _llm_error_count
    _llm_error_count = 0


# A candidate that's actually the right posting can otherwise be lost to a
# single unexplained failure (a timeout, a dropped connection, a truncated
# response) with no way to distinguish it from a genuine rejection --
# confirmed in practice on a real run: the correct candidate was the very
# first one walked, its confirm+extract call failed outright with no error
# detail, and every remaining candidate was either wrong or unfetchable,
# so the whole item got skipped for a reason that had nothing to do with
# whether it was a match. A transient failure is worth a retry, but
# repeating indefinitely would just burn budget against a real, persistent
# problem. (The anthropic client also retries connection errors/429/5xx
# itself, 2x by default, below this.)
#
# This constant is the default for single-shot callers
# (_classify_work_location), where one call is the whole listing's spend on
# that call anyway. It is NOT what bounds retries inside a candidate walk
# -- confirmed real cost risk if it were: a walk can attempt up to
# MAX_FALLBACK_CANDIDATES calls, and retrying every one of them
# independently would let a single bad batch (several candidates all
# hitting transient errors) multiply cost well past "one retry." Candidate
# walk calls pass max_attempts=1 here instead, and walk_candidates()
# manages one shared retry budget for the whole listing -- see there.
CLAUDE_RETRY_ATTEMPTS = 2
CLAUDE_RETRY_BACKOFF_S = 2.0


async def _run_claude_json(
    prompt: str,
    schema: dict,
    *,
    max_attempts: int = CLAUDE_RETRY_ATTEMPTS,
) -> Optional[dict]:
    """Single-shot structured-output call; None on failure. max_attempts
    defaults to CLAUDE_RETRY_ATTEMPTS (retry once) for single-shot callers
    (_classify_work_location), where "per call" and "per listing" are the
    same thing anyway. Callers inside a candidate walk
    (_try_confirmed_extraction, via _confirm_and_extract) pass
    max_attempts=1 here and manage their own single retry budget for the
    whole walk instead -- see walk_candidates()."""
    for attempt in range(1, max_attempts + 1):
        result = await _run_claude_json_once(prompt, schema)
        if result is not None:
            return result
        if attempt < max_attempts:
            logger.info("Retrying Claude call (attempt %d/%d)", attempt + 1, max_attempts)
            await asyncio.sleep(CLAUDE_RETRY_BACKOFF_S)
    return None


async def _run_claude_json_once(prompt: str, schema: dict) -> Optional[dict]:
    # Raw Anthropic SDK, structured output via output_config, no tools and
    # no thinking. Langfuse tracing is automatic (AnthropicInstrumentor, via
    # the observability.tracing import above) -- nothing to log by hand.
    try:
        response = await _client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=CLAUDE_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
    except anthropic.APIError as e:
        logger.info("Claude call failed: %s: %s", type(e).__name__, e)
        _record_llm_error()
        return None
    _record_cost(response.usage)

    text = next((b.text for b in response.content if b.type == "text"), None)
    if response.stop_reason == "max_tokens" or text is None:
        logger.info("Claude response unusable (stop_reason=%s)", response.stop_reason)
        _record_llm_error()
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        logger.info("Claude response was not valid JSON")
        _record_llm_error()
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
    otherwise have nothing usable to fetch. Its /details/{id}
    page serves the same JD directly with no such gate -- also confirmed in
    practice, on multiple postings, using the exact query string Adzuna's
    own API returned (not a separately-generated one). Swap to it when the
    pattern matches; any other URL (including every digest-source candidate,
    which is never adzuna.com) passes through unchanged."""
    return ADZUNA_LAND_AD_RE.sub(r"\1/details/\2", url, count=1)


async def _fetch_tiered(url: str) -> Optional[PageFetch]:
    """Tier 1 (plain fetch), escalating to tier 2 (headless render) if the
    result looks too thin. Used for both Adzuna's redirect_url and each
    digest-source search-result candidate -- one fetch pipeline, not two."""
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


def _extract_metadata_title(soup: BeautifulSoup) -> Optional[str]:
    """The JobPosting JSON-LD node's own `title` field, when present --
    confirmed necessary, not redundant with the raw HTML <title> tag: on a
    JS-shell page (Workday-hosted ones, same case _extract_metadata_text()
    exists for), the client sets the real page title after hydration, so a
    plain fetch's <title> tag comes back empty even though the same
    JSON-LD block that supplies the JD text also has the real role title
    sitting right in it. Checked first in _extract_text(); the raw <title>
    tag is the fallback for pages that don't embed JobPosting data at all."""
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
            title = node.get("title")
            if title:
                return str(title)
    return None


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
    # JobPosting JSON-LD's own title first -- the raw <title> tag comes
    # back empty on JS-shell pages (confirmed: a real Workday posting had
    # an empty <title> but a populated JSON-LD title field) -- see
    # _extract_metadata_title()'s docstring.
    page_title = _extract_metadata_title(soup) or (
        soup.title.get_text(strip=True) if soup.title else None
    )
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    visible_text = _extract_visible_text(html, soup)
    text = f"{metadata_text}\n\n{visible_text}" if metadata_text and visible_text else (metadata_text or visible_text)
    return PageFetch(
        text=text, metadata_text=metadata_text, remote_badge=remote_badge, page_title=page_title
    )


def _is_usable_length(text: Optional[str]) -> bool:
    return bool(text) and len(text) >= MIN_USABLE_CHARS


async def _confirm_and_extract(
    text: str,
    reference: PostingReference,
    page_title: Optional[str] = None,
    max_attempts: int = 1,
) -> Optional[dict]:
    """Judge and extract in a single call, not two -- both operate on the
    same page text, so this halves the LLM round-trips per candidate versus
    a separate confirm-then-extract pass. If the page turns out to be gated
    or the wrong posting, the model is told to leave the extraction fields
    empty/false rather than guess.

    page_title (the raw <title> HTML tag, separate from the extracted body
    text) is passed as an extra deterministic signal -- confirmed necessary
    in practice, not a nice-to-have: trafilatura's main-content extraction
    strips page headings as chrome, so a JD's body text often never
    restates the role title at all. Without this, a real run confirmed a
    "Staff Software Engineer" page as a match for a "Manager" reference
    title, since the body text never said either role name and the
    reference had no location/snippet to cross-check against (a source
    with only title+company, like agents/digest_source.py -- see
    CLAUDE.md's Phase 4 section).

    The "listing page is not the posting" sentence in the prompt was added
    after testing the raw-SDK switch: with the original wording, a generic
    company listings page (which names the reference role among many) was
    confirmed as the posting on about half of repeated runs -- on the
    claude_agent_sdk path too, not just the raw one. Stating it outright
    made it reject consistently (4/4), with no thinking needed."""
    prompt = (
        "Judge the page text below against this reference job posting, "
        "then extract from it if -- and only if -- it's a genuine match.\n\n"
        "First, judge whether the page is (a) actually showing content "
        "(not a login wall, paywall, or bot-block) and (b) describing the "
        "same specific job posting as the reference, not just a company's "
        "careers page in general. A page that lists several job openings, a "
        "search-results page, or a company careers index is NOT the posting "
        "itself, even if the reference role appears in the list -- only treat "
        "the page as the same posting if it contains that role's own full job "
        "description (responsibilities, requirements), not just its title or a "
        "link to it. Treat the page's own <title> tag (given "
        "below, separate from the page text) as a strong signal: if it "
        "names a substantially different role or seniority level than the "
        "reference title (e.g. \"Staff Engineer\" vs \"Manager\", "
        "\"Senior\" vs \"Director\"), this is very likely not the same "
        "posting even if the page's body content is topically similar.\n\n"
        f"Reference title: {reference.title}\n"
        f"Reference company: {reference.company}\n"
        f"Reference location: {reference.location or 'unknown'}\n"
        f"Reference description snippet: {reference.description or '(none)'}\n\n"
        f"Page <title> tag: {page_title or '(none)'}\n\n"
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
    return await _run_claude_json(
        prompt, CONFIRM_AND_EXTRACT_SCHEMA, max_attempts=max_attempts
    )


def _normalize_work_location(value: Any) -> Optional[str]:
    return value if value in ("remote", "hybrid", "onsite") else None


async def _classify_work_location(text: str) -> Optional[str]:
    """Minimal, output-bounded LLM call: classify remote/hybrid/onsite from
    JD text alone, nothing else. Output is a single enum word, so cost
    stays low even though it's a real LLM call -- unlike the rest of
    capture(), which makes none at all. Only
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


@dataclass
class ConfirmResult:
    extraction: Optional[dict]
    # True only when the LLM call itself failed outright (error, timeout) --
    # distinct from a call that succeeded and legitimately rejected the
    # candidate (wrong posting, gated). walk_candidates() uses this to
    # decide which candidates are worth its one shared retry; a legitimate
    # rejection is never worth retrying, since retrying won't change the
    # judgment.
    call_failed: bool


async def _try_confirmed_extraction(
    text: Optional[str],
    reference: PostingReference,
    page_title: Optional[str] = None,
    max_attempts: int = 1,
) -> ConfirmResult:
    """Confirm identity and extract in one call. Used for search-result
    candidates only -- external pages (search results or a company's
    careers site) whose identity is genuinely uncertain, unlike Adzuna's
    primary URL, which is handled deterministically in capture() (see there
    for why).
    ConfirmResult.extraction is the result dict (usable directly as an
    extraction -- full_description/salary_* fields) or None if the text
    was too short, the call failed, or the page was rejected as gated /
    not the same posting -- ConfirmResult.call_failed distinguishes the
    "call failed" case from the other two for walk_candidates()'s retry
    budget."""
    if not _is_usable_length(text):
        logger.info(
            "Skipping confirm+extract for %s -- text too short/missing (%d chars)",
            reference.title, len(text) if text else 0,
        )
        return ConfirmResult(extraction=None, call_failed=False)
    result = await _confirm_and_extract(text, reference, page_title, max_attempts=max_attempts)
    if result is None:
        logger.info("Confirm+extract call failed for %s", reference.title)
        return ConfirmResult(extraction=None, call_failed=True)
    if result["gated"]:
        logger.info(
            "Confirm+extract rejected candidate for %s: judged gated (%s)",
            reference.title, result.get("reason"),
        )
        return ConfirmResult(extraction=None, call_failed=False)
    if not result["same_posting"]:
        logger.info(
            "Confirm+extract rejected candidate for %s: not judged the same posting (%s)",
            reference.title, result.get("reason"),
        )
        return ConfirmResult(extraction=None, call_failed=False)
    logger.info("Confirm+extract passed for %s", reference.title)
    return ConfirmResult(extraction=result, call_failed=False)


async def walk_candidates(
    candidate_urls: list[str], reference: PostingReference
) -> Optional[CandidateMatch]:
    """Fetch each candidate URL in order through the tiered fetch, running
    confirm+extract on each, stopping at the first that passes. Used by
    capture_from_search() (agents/digest_source.py's candidates). Caller is
    responsible for capping candidate_urls to MAX_FALLBACK_CANDIDATES
    before calling.

    One retry budget for the *whole* walk, not one per candidate -- see
    CLAUDE_RETRY_ATTEMPTS's comment for why per-candidate retries were
    confirmed as a real cost risk. Every candidate's confirm+extract call
    gets a single attempt (max_attempts=1); if nothing confirms and at
    least one candidate's call failed outright (not just a legitimate
    rejection), the earliest-ranked such candidate gets exactly one retry
    -- earliest-ranked because search ranking correlates with likelihood
    of being correct, so that's the candidate most worth spending the
    retry on."""
    retry_candidate: Optional[tuple[str, PageFetch]] = None

    for candidate_url in candidate_urls[:MAX_FALLBACK_CANDIDATES]:
        candidate_page = await _fetch_tiered(candidate_url)
        result = await _try_confirmed_extraction(
            candidate_page.text if candidate_page else None,
            reference,
            candidate_page.page_title if candidate_page else None,
        )
        if result.extraction is not None:
            return CandidateMatch(
                url=candidate_url,
                extraction=result.extraction,
                remote_badge=candidate_page.remote_badge if candidate_page else None,
            )
        if result.call_failed and retry_candidate is None and candidate_page is not None:
            retry_candidate = (candidate_url, candidate_page)

    if retry_candidate is not None:
        candidate_url, candidate_page = retry_candidate
        logger.info("Retrying the one call failure in this walk: %s", candidate_url)
        result = await _try_confirmed_extraction(
            candidate_page.text, reference, candidate_page.page_title
        )
        if result.extraction is not None:
            return CandidateMatch(
                url=candidate_url,
                extraction=result.extraction,
                remote_badge=candidate_page.remote_badge,
            )

    return None


def _parse_salary_from_extraction(
    extraction: dict, title: str
) -> tuple[Optional[Decimal], Optional[Decimal], Optional[bool]]:
    """Parses confirm+extract's salary_found/salary_min/salary_max fields.
    Only capture_from_search() gets these -- Adzuna's own page has no
    independent salary to check against Adzuna's fields (see "Salary
    detection" in CLAUDE.md)."""
    if not extraction.get("salary_found"):
        return None, None, None
    try:
        salary_min = Decimal(str(extraction["salary_min"]))
        salary_max_value = extraction["salary_max"]
        salary_max = Decimal(str(salary_max_value)) if salary_max_value is not None else salary_min
        return salary_min, salary_max, False
    except (TypeError, ArithmeticError):
        logger.warning("Malformed salary in extraction for %s; ignoring", title)
        return None, None, None


@observe(name="capture_posting")
async def capture(adzuna: AdzunaResult) -> CaptureResult:
    """Best-effort full posting capture for an Adzuna result. Always returns
    a result -- degrades to Adzuna's snippet rather than raising.

    Tier 1/2 only. Adzuna's own /details/{id} page (after URL
    normalization) embeds a JobPosting JSON-LD block with a clean,
    already-isolated description; its presence is the validity gate and its
    absence means the listing expired or the page isn't the real posting --
    confirmed more reliable in practice than judging visible text. There is
    deliberately no tier 3 here anymore: the old search-and-walk fallback
    (a web search plus up to 10 confirm+extract calls per posting) was
    removed because tier 1/2 resolves nearly every Adzuna posting, so it
    almost never ran and was the most expensive code path to keep. When
    tier 1/2 does fail, this logs a warning and keeps Adzuna's snippet
    (description_source='adzuna_snippet') -- the human can open the original
    redirect_url. See CLAUDE.md's Phase 1 capture section.
    """
    normalized_url = _normalize_redirect_url(adzuna.redirect_url)
    if normalized_url != adzuna.redirect_url:
        logger.info(
            "Tier 1/2: normalized redirect_url for %s: %s -> %s",
            adzuna.title, adzuna.redirect_url, normalized_url,
        )

    logger.info("Tier 1/2: trying %s -> %s", adzuna.title, normalized_url)
    page: Optional[PageFetch] = None
    try:
        page = await _fetch_tiered(normalized_url)
    except Exception:
        logger.exception("Tier 1/2 capture failed for %s", normalized_url)

    if page is None or not _is_usable_length(page.metadata_text):
        logger.warning(
            "Tier 1/2 found no usable JobPosting JSON-LD for %s at %s (%s) -- "
            "keeping Adzuna's snippet, no fallback search",
            adzuna.title, adzuna.company, adzuna.redirect_url,
        )
        return CaptureResult(
            description=adzuna.description or "",
            description_source="adzuna_snippet",
            url=adzuna.redirect_url,
            salary_min=adzuna.salary_min,
            salary_max=adzuna.salary_max,
            salary_is_predicted=adzuna.salary_is_predicted,
            work_location=None,
        )

    logger.info(
        "Tier 1/2: deterministic JD from JobPosting JSON-LD for %s (%d chars)",
        adzuna.title, len(page.metadata_text),
    )

    # Adzuna's own REMOTE badge (deterministic, when present) wins outright
    # -- a direct site-provided signal, not a guess. Otherwise a minimal,
    # output-bounded classification call answers remote/hybrid/onsite from
    # the JD text alone (one enum word out).
    if page.remote_badge:
        work_location = "remote"
    else:
        work_location = await _classify_work_location(page.metadata_text)

    # Salary always comes from Adzuna's own fields: this page IS Adzuna's
    # data source, so there's no independent JD here to check them against.
    return CaptureResult(
        description=page.metadata_text,
        description_source="redirect_url",
        url=normalized_url,
        salary_min=adzuna.salary_min,
        salary_max=adzuna.salary_max,
        salary_is_predicted=adzuna.salary_is_predicted,
        work_location=work_location,
    )


async def capture_from_search(
    reference: PostingReference, candidate_urls: list[str]
) -> Optional[CaptureResult]:
    """Tier-3-only capture for sources with no redirect_url and no Adzuna
    snippet/salary/remote-badge fallback -- agents/digest_source.py's use
    case. candidate_urls comes from the caller's own search
    (agents/digest_search.py -- see CLAUDE.md's Phase 4 section). Reuses
    walk_candidates() and _parse_salary_from_extraction().

    Returns None if no candidate confirms -- unlike capture(), there's no
    snippet to degrade to here, so callers should skip the item entirely
    rather than write a row (see CLAUDE.md: "this degrades the same way
    tier 3 degrading does -- skip, don't block the run")."""
    match = await walk_candidates(candidate_urls, reference)
    if match is None:
        return None

    extraction = match.extraction
    salary_min, salary_max, salary_is_predicted = _parse_salary_from_extraction(
        extraction, reference.title
    )

    if match.remote_badge:
        work_location = "remote"
    else:
        work_location = _normalize_work_location(extraction.get("work_location"))

    return CaptureResult(
        description=extraction["full_description"],
        description_source="company_site",
        url=match.url,
        salary_min=salary_min,
        salary_max=salary_max,
        salary_is_predicted=salary_is_predicted,
        work_location=work_location,
    )
