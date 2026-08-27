"""FastMCP server exposing Adzuna search as a tool.

No dedicated ATS lookup tools -- see CLAUDE.md's "Why no dedicated
Greenhouse/Lever/Ashby tools in Phase 1" for why. This is the only tool
Phase 1 needs.
"""

import os
import time
from typing import Optional

import requests
from dotenv import load_dotenv
from fastmcp import FastMCP

from models.schema import AdzunaResult

load_dotenv(override=True)

APP_ID = os.environ["ADZUNA_APP_ID"]
APP_KEY = os.environ["ADZUNA_APP_KEY"]
COUNTRY = os.environ.get("ADZUNA_COUNTRY", "gb")

BASE_URL = f"https://api.adzuna.com/v1/api/jobs/{COUNTRY}"

# Adzuna occasionally returns a transient 5xx -- confirmed in practice: a
# 503 that cleared on its own within seconds on retry, same request,
# unchanged params. Only 5xx is retried; a 4xx means something's actually
# wrong with the request (e.g. the salary_min float-serialization bug
# found earlier), and retrying that would just repeat the same failure.
RETRYABLE_STATUS_CODES = {500, 502, 503, 504}
MAX_ATTEMPTS = 3
RETRY_BACKOFF_S = 1.0

mcp = FastMCP("job_sources")


def _get_with_retry(url: str, params: dict) -> requests.Response:
    response = requests.get(url, params=params)
    attempt = 1
    while response.status_code in RETRYABLE_STATUS_CODES and attempt < MAX_ATTEMPTS:
        time.sleep(RETRY_BACKOFF_S * attempt)
        response = requests.get(url, params=params)
        attempt += 1
    return response


def _search_params(
    what: str,
    where: str,
    max_days_old: int,
    salary_min: Optional[float],
    results_per_page: int,
    title_only: Optional[str],
    full_time: int,
) -> dict:
    params = {
        "app_id": APP_ID,
        "app_key": APP_KEY,
        "what": what,
        "where": where,
        "max_days_old": max_days_old,
        "results_per_page": results_per_page,
        "full_time": full_time,
    }
    if salary_min is not None:
        # Adzuna rejects salary_min serialized with a decimal point (e.g.
        # "180000.0") with a 400 -- confirmed in practice -- even though
        # it's accepted as a Python float everywhere on this side. Cast to
        # int at the API boundary so callers can still pass a float.
        params["salary_min"] = int(salary_min)
    if title_only is not None:
        params["title_only"] = title_only
    return params


@mcp.tool
def adzuna_search(
    what: str,
    where: str = "",
    max_days_old: int = 14,
    salary_min: Optional[float] = None,
    results_per_page: int = 20,
    page: int = 1,
    title_only: Optional[str] = None,
    full_time: int = 1,
) -> list[AdzunaResult]:
    """Search Adzuna job postings.

    Args:
        what: Keyword(s) to search for, e.g. "data scientist".
        where: Location, e.g. "united states".
        max_days_old: Only return postings at most this many days old.
        salary_min: Minimum salary filter, if any.
        results_per_page: Results per page (each page is one API hit).
        page: Which page of results to fetch.
        title_only: If set, restrict the search to this phrase appearing in
            the posting's title specifically (in addition to `what`).
        full_time: 1 to filter to full-time postings only, 0 for no filter.
    """
    params = _search_params(
        what, where, max_days_old, salary_min, results_per_page, title_only, full_time
    )
    response = _get_with_retry(f"{BASE_URL}/search/{page}", params)
    response.raise_for_status()
    raw_results = response.json().get("results", [])
    return [AdzunaResult.model_validate(r) for r in raw_results]


@mcp.tool
def adzuna_count(
    what: str,
    where: str = "",
    max_days_old: int = 14,
    salary_min: Optional[float] = None,
    title_only: Optional[str] = None,
    full_time: int = 1,
) -> int:
    """Get the total number of matching Adzuna postings for a query, without
    fetching any actual results -- one cheap API hit (results_per_page=0
    still returns the true total in the response's `count` field).

    Args:
        what: Keyword(s) to search for, e.g. "data scientist".
        where: Location, e.g. "united states".
        max_days_old: Only return postings at most this many days old.
        salary_min: Minimum salary filter, if any.
        title_only: If set, restrict the search to this phrase appearing in
            the posting's title specifically (in addition to `what`).
        full_time: 1 to filter to full-time postings only, 0 for no filter.
    """
    params = _search_params(
        what, where, max_days_old, salary_min, 0, title_only, full_time
    )
    response = _get_with_retry(f"{BASE_URL}/search/1", params)
    response.raise_for_status()
    return response.json().get("count", 0)


if __name__ == "__main__":
    mcp.run()
