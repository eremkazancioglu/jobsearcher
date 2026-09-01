"""Exploration script for The Muse Jobs API v2 -- kept as the reference
for a real, thorough investigation into using The Muse as a second
discovery source, which concluded (2026-09-01) that it isn't worth
integrating -- see CLAUDE.md's "Deliberately ruled out" list under
Sourcing strategy for the full reasoning. Parked, not deleted, in case
the API changes (date filtering or a posting-status field would flip the
assessment) -- unlike explore_adzuna.py, which was deleted once its job
(informing a real integration) was done, this one's job was to inform a
decision not to integrate, so it stays as the record of why.

    uv run explore_muse.py
    MUSE_API_KEY=your_key uv run explore_muse.py   # registered key, higher rate limit

Docs: https://www.themuse.com/developers/api/v2

Confirmed in practice (2026-08-31 to 2026-09-01), not assumed from the docs alone:
- The endpoint is `https://www.themuse.com/api/public/jobs`, not the
  legacy `api-v2.themuse.com` (still works but redirects; docs recommend
  the new URL).
- `results[].contents` is the FULL job description as HTML -- a real
  structural difference from Adzuna, which only ever gives a snippet
  until captured. If this holds up with a real API key too, The Muse
  might not need Adzuna's tiered capture machinery at all.
- No salary field anywhere in the response, unlike Adzuna's
  salary_min/salary_max/salary_is_predicted.
- `results[].locations` is a list (a posting can have several), not a
  single location like Adzuna's.
- `results[].type` is "native" or "external" -- unclear yet whether
  "external" means `refs.landing_page` sends you off themuse.com; worth
  checking once real postings are visible.
- Unauthenticated access DOES return the real, live, current dataset
  (409,837 total matches with no filters, dates into 2026) -- initial
  testing here mistakenly concluded otherwise, because...
- **`category` values must match The Muse's own fixed taxonomy exactly --
  "Data Science" is NOT a valid value** (it silently matched almost
  nothing: 2 stale results, both incidentally tagged that as legacy
  freeform text). The real category is **"Data and Analytics"**
  (also relevant: "Software Engineering", "Science and Engineering").
  Confirmed by pulling `categories[].name` across several unfiltered
  pages -- see `discover_categories()` below. Don't assume a category
  string from the job title/domain; verify it against real data first.
- **No keyword/title search param exists at all** -- confirmed by
  sending `q=` and `keyword=` and getting back the exact same `total`
  (409,837) as no filter, meaning both were silently ignored rather than
  applied. Unlike Adzuna's `what`/`title_only`, narrowing by keyword here
  has to happen client-side, after fetching -- see
  `search_with_keywords()` below. This makes keyword search meaningfully
  more expensive than on Adzuna: `category` alone is broad (18,444 total
  for "Data and Analytics"), so finding a tight set of matches means
  walking real pages of full results (each with full HTML descriptions,
  not a cheap snippet) rather than one narrow, server-filtered query.
- **No date filter param exists either** -- `days`, `since`,
  `posted_since`, `start_date`, `max_days_old`, `date_from` all silently
  ignored (identical `total` with or without). Worse than the keyword
  case: results also AREN'T sorted by `publication_date` at all, with or
  without `descending=true`. The docs describe `descending` as
  "intelligently sorted by a number of factors such as trendiness,
  uniqueness, newness, etc." -- a blended relevance ranking, not a pure
  chronological sort, which matches what's observed: one page's dates
  span well over a year in no particular order, `true` and `false` both.
  **This was re-confirmed with a real registered API key, not just
  unauthenticated/test access** -- same identical unsorted-by-date
  results either way, and the key genuinely is live (12,000 req/hr
  returned in `X-RateLimit-Limit`, higher than the docs' stated
  3,600/hr for registered apps -- so this isn't a "the key unlocks
  proper sorting" situation; it's a real, permanent API limitation, not
  a test-tier artifact). So unlike keyword filtering, there's no way to
  bias a page walk toward recent postings -- `search_recent()` below is
  a straightforward client-side filter, but be aware it may need to walk
  a large, unpredictable number of pages to find a meaningful "last X
  days" set, since recent postings aren't concentrated anywhere in the
  result order. Genuinely worse than Adzuna here, not just
  differently-shaped -- this is the central reason to hesitate before
  building a real integration against this API for a daily-discovery use
  case: there's no way to efficiently ask "what's new since last time."
- **Pagination is hard-capped at page 99 (2,000 results max), globally,
  for any query regardless of how many total results actually match** --
  confirmed directly: page 99 returns 200 OK, page 100 returns
  400 {"code": 400, "error": "Value 'page' is too high"}, same cap
  whether unfiltered or filtered by category. A broad category (e.g.
  "Data and Analytics" alone: 18,389 total) leaves ~89% of its own
  results permanently unreachable no matter how you page -- not a cost or
  rate-limit problem, a hard wall. A narrowed query (adding
  `location=Flexible/Remote`: 995 total) fits under the ceiling today,
  but combined with the lack of date-based sorting above, there's no
  guarantee it stays there, and no graceful way to notice besides
  watching `total` over time.
- **No way to tell a live posting from a closed one** -- no status field
  anywhere on the job object (confirmed by inspecting a full raw record),
  and old postings are never purged: the oldest posting in a 995-result
  walk was 1,384 days old (2022-11-16). Manually checked several of the
  oldest via their `landing_page` links -- confirmed genuinely stale/
  closed, not still open, indistinguishable from a live posting in the
  API response itself.
- The Companies API (`GET /api/public/companies`, `industry`/`location`/
  `size` filters, all confirmed real server-side filtering) was
  investigated as a way to pre-filter which companies' jobs to search,
  but doesn't address any of the above and measurably hurt recall:
  excluding "Large Size" companies dropped 38 real "data scientist"
  matches to 3, since Muse's size bucketing lumps well-known
  (Zoom/Atlassian/Zillow-scale) companies in with genuine mega-corps, and
  doesn't track "startup-feeling" the way `categorize.py`'s LLM judgment
  against the real posting text already does, better, downstream.

**Conclusion (2026-09-01): parked, not integrated.** The free full job
description is real value, but doesn't offset having no way to find
what's new, a hard ceiling that only gets worse over time, and no way to
filter out dead postings. See CLAUDE.md's Sourcing strategy section for
the full reasoning and the ToS judgment call also made along the way.
Revisit if the API adds date filtering or a posting-status field.
"""

import json
import os
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

load_dotenv(override=True)

BASE_URL = "https://www.themuse.com/api/public/jobs"
API_KEY = os.environ.get("MUSE_API_KEY")


def search(page: int = 0, category: str | None = None, location: str | None = None, level: str | None = None) -> dict:
    params = {"page": page}
    if category:
        params["category"] = category
    if location:
        params["location"] = location
    if level:
        params["level"] = level
    if API_KEY:
        params["api_key"] = API_KEY
    response = requests.get(BASE_URL, params=params, timeout=15)
    response.raise_for_status()
    return response.json()


def discover_categories(pages: int = 5) -> set[str]:
    """Pull real category values off actual results rather than guessing
    from job titles/domains -- "Data Science" looks like an obviously
    correct guess and is wrong (see module docstring)."""
    categories = set()
    for page in range(pages):
        data = search(page=page)
        for job in data["results"]:
            categories.update(c["name"] for c in job["categories"])
    return categories


def matches_keywords(job: dict, keywords: list[str], title_only: bool = True) -> bool:
    text = job["name"] if title_only else job["name"] + " " + job["contents"]
    text = text.lower()
    return any(kw.lower() in text for kw in keywords)


def search_with_keywords(
    keywords: list[str],
    category: str | None = None,
    location: str | None = None,
    title_only: bool = True,
    max_pages: int = 10,
) -> list[dict]:
    """Client-side keyword filter, walking real pages -- see module
    docstring for why: no server-side keyword param exists at all.
    max_pages bounds the walk (no silent caps, but bounded -- same
    pattern as discovery.py's --max-pages for Adzuna): logs explicitly
    when there are more pages than max_pages covers, rather than quietly
    treating "found nothing in what we walked" as "there's nothing"."""
    first_page = search(page=0, category=category, location=location)
    total_pages = first_page["page_count"]
    pages_to_walk = min(max_pages, total_pages)
    print(
        f"Walking {pages_to_walk} of {total_pages} page(s) "
        f"({first_page['total']} total match(es) before keyword filtering)"
    )
    if total_pages > max_pages:
        print(f"  -- {total_pages - max_pages} page(s) beyond max_pages={max_pages} were not walked")

    matches = []
    for page in range(pages_to_walk):
        data = search(page=page, category=category, location=location)
        matches.extend(job for job in data["results"] if matches_keywords(job, keywords, title_only))
    return matches


def matches_recent(job: dict, max_days_old: int) -> bool:
    published = datetime.fromisoformat(job["publication_date"].replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - published) <= timedelta(days=max_days_old)


def search_recent(
    max_days_old: int,
    category: str | None = None,
    location: str | None = None,
    keywords: list[str] | None = None,
    title_only: bool = True,
    max_pages: int = 10,
) -> list[dict]:
    """Client-side recency filter (optionally combined with a keyword
    filter in the same walk) -- see module docstring: no date param
    exists, AND results aren't sorted by publication_date at all, so
    unlike search_with_keywords() this walk isn't even biased toward
    finding what it's looking for. max_pages bounds it the same way, but
    treat a low/zero match count here with real suspicion -- it may mean
    "not many recent postings in this category" or it may just mean the
    recent ones happened to land on pages beyond max_pages. Print the
    walked-vs-total ratio and decide accordingly, don't assume completeness."""
    first_page = search(page=0, category=category, location=location)
    total_pages = first_page["page_count"]
    pages_to_walk = min(max_pages, total_pages)
    print(
        f"Walking {pages_to_walk} of {total_pages} page(s) -- recency isn't sorted, "
        f"so this is not a representative sample of recent postings, just what happened to be here"
    )
    if total_pages > max_pages:
        print(f"  -- {total_pages - max_pages} page(s) beyond max_pages={max_pages} were not walked")

    matches = []
    for page in range(pages_to_walk):
        data = search(page=page, category=category, location=location)
        for job in data["results"]:
            if not matches_recent(job, max_days_old):
                continue
            if keywords and not matches_keywords(job, keywords, title_only):
                continue
            matches.append(job)
    return matches


def summarize(data: dict) -> None:
    print(
        f"page {data['page']} of {data['page_count']} -- {data['total']} total "
        f"match(es), {data['items_per_page']}/page"
    )
    for r in data["results"]:
        locations = ", ".join(loc["name"] for loc in r["locations"]) or "unspecified"
        categories = ", ".join(c["name"] for c in r["categories"])
        levels = ", ".join(lv["name"] for lv in r["levels"])
        print(f"- [{r['id']}] {r['name']} @ {r['company']['name']} ({r['type']})")
        print(f"    published: {r['publication_date']} | location(s): {locations}")
        print(f"    category: {categories} | level: {levels}")
        print(f"    url: {r['refs']['landing_page']}")
        print(f"    contents: {len(r['contents'])} chars of HTML")


if __name__ == "__main__":
    if not API_KEY:
        print(
            "No MUSE_API_KEY set -- using unauthenticated/test access (500 req/hr per the "
            "docs). Confirmed in practice: this still returns the real, live dataset (400K+ "
            "total matches, current dates) -- register at "
            "https://www.themuse.com/developers/api/v2 anyway before relying on this "
            "for real use, since the docs say registration is required beyond testing and "
            "the higher rate limit (3,600/hr) matters at any real volume.\n"
        )
    else:
        probe = requests.get(BASE_URL, params={"page": 0, "api_key": API_KEY}, timeout=15)
        print(
            f"MUSE_API_KEY set -- authenticated (rate limit: "
            f"{probe.headers.get('X-RateLimit-Limit', '?')}/hr, "
            f"{probe.headers.get('X-RateLimit-Remaining', '?')} remaining this window).\n"
        )

    print("=== Basic search, no filters ===")
    summarize(search())

    print("\n=== Real category values (from actual results, not guessed) ===")
    print(sorted(discover_categories()))

    print("\n=== category=Data and Analytics ===")
    summarize(search(category="Data and Analytics"))

    print("\n=== category=Data and Analytics, location=Flexible / Remote ===")
    summarize(search(category="Data and Analytics", location="Flexible / Remote"))

    print("\n=== Raw shape of first Data and Analytics result (for field reference) ===")
    data = search(category="Data and Analytics")
    if data["results"]:
        print(json.dumps(data["results"][0], indent=2))
    else:
        print("(no results to show)")

    print('\n=== Client-side keyword filter: "data scientist" in title, category=Data and Analytics, remote ===')
    keyword_matches = search_with_keywords(
        ["data scientist"], category="Data and Analytics", location="Flexible / Remote", max_pages=10
    )
    print(f"{len(keyword_matches)} title match(es) found:")
    for job in keyword_matches:
        print(f"- [{job['id']}] {job['name']} @ {job['company']['name']}")
        print(f"    {job['refs']['landing_page']}")

    print('\n=== Client-side recency filter: last 3 days, "data scientist" in title, category=Data and Analytics ===')
    recent_matches = search_recent(
        max_days_old=3, category="Data and Analytics", keywords=["data scientist"], max_pages=10
    )
    print(f"{len(recent_matches)} match(es) found:")
    for job in recent_matches:
        print(f"- [{job['id']}] {job['name']} @ {job['company']['name']} ({job['publication_date']})")
        print(f"    {job['refs']['landing_page']}")
