"""Tier 1/2/3 full-posting capture, plus LLM confirm/extract judgment calls.

Given an Adzuna search result (a title, company, location, description
snippet, and a redirect_url), try to recover the full job posting:

    Tier 1 -- plain fetch of redirect_url.
    Tier 2 -- headless render of redirect_url, if tier 1's text is too short.
    Tier 3 -- fallback web search for the company's own careers page.

Each candidate page is judged by an LLM for (a) whether it's actually
showing content (not a login/paywall) and (b) whether it's the same posting
Adzuna described, before its text is trusted for extraction. If nothing
works, capture() degrades gracefully and keeps Adzuna's snippet -- see
CLAUDE.md's "Full posting capture" section for the full rationale.

This flow is kept independent of the Adzuna call that produced the result:
it's handed plain facts (company, title, location, snippet) and
independently looks for a public posting, never touching Adzuna's
redirect_url as anything other than "a URL to fetch."
"""

import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

import requests
from bs4 import BeautifulSoup
from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query
from playwright.async_api import async_playwright

from models.schema import AdzunaResult, DescriptionSource

logger = logging.getLogger(__name__)

MIN_USABLE_CHARS = 500
FETCH_TIMEOUT_S = 15
RENDER_TIMEOUT_MS = 20_000

# Confirm/extract/fallback-search are all narrow judgment/lookup tasks, not
# deep reasoning -- Haiku is plenty, and vastly cheaper than the CLI's
# default model, which matters a lot for tier 3 (a multi-turn agentic loop
# with web search/fetch tool calls, run once per posting that needs it).
CLAUDE_MODEL = "claude-haiku-4-5-20251001"
# Hard per-call ceiling so one runaway agentic loop (e.g. tier 3 down a
# rabbit hole of dead-end searches) can't blow through real money.
CLAUDE_MAX_BUDGET_USD = 0.15
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

CONFIRM_SCHEMA = {
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
    },
    "required": ["gated", "same_posting", "reason"],
    "additionalProperties": False,
}

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "full_description": {"type": "string"},
        "salary_found": {
            "type": "boolean",
            "description": "true only if the posting text itself states a salary or range",
        },
        "salary_min": {"type": ["number", "null"]},
        "salary_max": {
            "type": ["number", "null"],
            "description": "if the posting states a single figure, set this equal to salary_min",
        },
    },
    "required": ["full_description", "salary_found", "salary_min", "salary_max"],
    "additionalProperties": False,
}

FALLBACK_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean"},
        "url": {"type": ["string", "null"]},
        "page_text": {
            "type": ["string", "null"],
            "description": "the full visible text of the located posting page",
        },
    },
    "required": ["found", "url", "page_text"],
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


CLAUDE_QUERY_TIMEOUT_S = 120


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
    )

    async def _run() -> Optional[dict]:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, ResultMessage):
                if message.is_error:
                    logger.warning("Claude query returned an error: %s", message.result)
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


def _fetch_plain(url: str) -> Optional[str]:
    try:
        response = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=FETCH_TIMEOUT_S,
        )
        response.raise_for_status()
    except requests.RequestException:
        logger.info("Tier 1 plain fetch failed for %s", url)
        return None
    return _extract_text(response.text)


async def _fetch_rendered(url: str) -> Optional[str]:
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                page = await browser.new_page(user_agent=USER_AGENT)
                await page.goto(
                    url, wait_until="networkidle", timeout=RENDER_TIMEOUT_MS
                )
                html = await page.content()
            finally:
                await browser.close()
    except Exception:
        logger.info("Tier 2 headless render failed for %s", url)
        return None
    return _extract_text(html)


def _extract_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    lines = (line.strip() for line in soup.get_text("\n").splitlines())
    return "\n".join(line for line in lines if line)


def _is_usable_length(text: Optional[str]) -> bool:
    return bool(text) and len(text) >= MIN_USABLE_CHARS


async def _confirm(text: str, adzuna: AdzunaResult) -> Optional[dict]:
    prompt = (
        "Judge whether the page text below is (a) actually showing content "
        "(not a login wall, paywall, or bot-block) and (b) describing the "
        "same specific job posting as this reference, not just a company's "
        "careers page in general.\n\n"
        f"Reference title: {adzuna.title}\n"
        f"Reference company: {adzuna.company}\n"
        f"Reference location: {adzuna.location or 'unknown'}\n"
        f"Reference description snippet: {adzuna.description or '(none)'}\n\n"
        f"Page text:\n{text[:12000]}"
    )
    return await _run_claude_json(prompt, CONFIRM_SCHEMA)


async def _extract(text: str) -> Optional[dict]:
    prompt = (
        "Extract the full job description from the page text below, and "
        "separately state whether the posting itself states a salary or "
        "salary range (do not infer or estimate one if it doesn't).\n\n"
        f"Page text:\n{text[:20000]}"
    )
    return await _run_claude_json(prompt, EXTRACT_SCHEMA)


async def _fallback_search(adzuna: AdzunaResult) -> Optional[dict]:
    prompt = (
        f"Find the official careers page for the company '{adzuna.company}' "
        f"(try searches like '{adzuna.company} careers' or "
        f"'{adzuna.company} jobs'), then use whatever that page offers "
        "(browsing, its own search box, filtering) to locate this specific "
        "open role:\n"
        f"Title: {adzuna.title}\n"
        f"Location: {adzuna.location or 'unknown'}\n"
        f"Description snippet (from a job board, to cross-check against): "
        f"{adzuna.description or '(none)'}\n\n"
        "If you find the specific posting page (not just the general "
        "careers/jobs listing page), fetch it and return found=true, its "
        "URL, and its full visible page text. If you can only find the "
        "general careers page or nothing relevant, return found=false."
    )
    return await _run_claude_json(
        prompt,
        FALLBACK_SEARCH_SCHEMA,
        allowed_tools=["WebSearch", "WebFetch"],
        permission_mode="bypassPermissions",
        max_turns=8,
    )


async def _try_confirmed_extraction(text: str, adzuna: AdzunaResult) -> Optional[dict]:
    """Run confirm, and if it passes, extract. Returns the extraction dict or None."""
    if not _is_usable_length(text):
        return None
    judgment = await _confirm(text, adzuna)
    if judgment is None or judgment["gated"] or not judgment["same_posting"]:
        return None
    return await _extract(text)


async def capture(adzuna: AdzunaResult) -> CaptureResult:
    """Best-effort full posting capture. Always returns a result -- degrades
    to Adzuna's snippet rather than raising when nothing works."""

    extraction = None
    description_source: DescriptionSource = "adzuna_snippet"
    url = adzuna.redirect_url

    try:
        text = _fetch_plain(adzuna.redirect_url)
        if not _is_usable_length(text):
            text = await _fetch_rendered(adzuna.redirect_url)
        if text is not None:
            extraction = await _try_confirmed_extraction(text, adzuna)
        if extraction is not None:
            description_source = "redirect_url"
    except Exception:
        logger.exception("Tier 1/2 capture failed for %s", adzuna.redirect_url)

    if extraction is None:
        try:
            fallback = await _fallback_search(adzuna)
            if fallback and fallback["found"] and fallback["page_text"]:
                fallback_extraction = await _try_confirmed_extraction(
                    fallback["page_text"], adzuna
                )
                if fallback_extraction is not None:
                    extraction = fallback_extraction
                    description_source = "company_site"
                    url = fallback["url"] or url
        except Exception:
            logger.exception("Tier 3 fallback search failed for %s", adzuna.title)

    salary_min, salary_max, salary_is_predicted = None, None, None
    if extraction is not None and extraction["salary_found"]:
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

    return CaptureResult(
        description=description,
        description_source=description_source,
        url=url,
        salary_min=salary_min,
        salary_max=salary_max,
        salary_is_predicted=salary_is_predicted,
    )
