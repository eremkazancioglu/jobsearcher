"""FastMCP server exposing Adzuna search as a tool.

No dedicated ATS lookup tools -- see CLAUDE.md's "Why no dedicated
Greenhouse/Lever/Ashby tools in Phase 1" for why. This is the only tool
Phase 1 needs.
"""

import os
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

mcp = FastMCP("job_sources")


@mcp.tool
def adzuna_search(
    what: str,
    where: str = "",
    max_days_old: int = 14,
    salary_min: Optional[float] = None,
    results_per_page: int = 20,
    page: int = 1,
) -> list[AdzunaResult]:
    """Search Adzuna job postings.

    Args:
        what: Keyword(s) to search for, e.g. "data scientist".
        where: Location, e.g. "united states".
        max_days_old: Only return postings at most this many days old.
        salary_min: Minimum salary filter, if any.
        results_per_page: Results per page (each page is one API hit).
        page: Which page of results to fetch.
    """
    params = {
        "app_id": APP_ID,
        "app_key": APP_KEY,
        "what": what,
        "where": where,
        "max_days_old": max_days_old,
        "results_per_page": results_per_page,
    }
    if salary_min is not None:
        params["salary_min"] = salary_min

    response = requests.get(f"{BASE_URL}/search/{page}", params=params)
    response.raise_for_status()
    raw_results = response.json().get("results", [])
    return [AdzunaResult.model_validate(r) for r in raw_results]


if __name__ == "__main__":
    mcp.run()
