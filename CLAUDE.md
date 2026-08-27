# Job hunt agents — project brief

A multi-agent pipeline that discovers job postings, assesses fit against a
resume, researches the companies behind good matches, and tracks application
status via email. Also a portfolio project — code quality and a clean
"clone and run" experience matter, not just working software.

This document is the spec to build from. Where a decision has a reason
behind it, the reason is included — several of these look like they could be
simplified further, but the simplification was already tried and rejected
for a specific reason. Don't re-derive from scratch; extend from here.

## How this document is organized

Two phases, built in order, not in parallel:

- **Phase 1** — discovery (Adzuna) and full posting capture. Build this
  completely and validate it against real postings before starting Phase 2.
- **Phase 2** — everything downstream of a captured posting: fit
  categorization, company research, the application tracker, the digest,
  the dashboard, and full observability. This depends on Phase 1's output
  (rows already sitting in `postings`) and extends it — it doesn't get
  built alongside Phase 1.

Everything outside the two phase sections (data model, rationale,
sourcing, scheduling, monitoring, environments, stack, repo layout, env
vars) is shared reference material both phases draw on, defined once so it
doesn't drift between them.

When Phase 1 is confirmed working, the instruction to continue is just
"go on with Phase 2" — the section below has what's needed.

## Data model (Postgres / Neon)

Defined once, in full, up front — Phase 1 creates this schema and starts
populating `postings`; Phase 2 populates the rest and starts writing
non-null `match_category` values. No migration between phases.

```sql
create extension if not exists pgcrypto;

create table postings (
    id uuid primary key default gen_random_uuid(),
    source text not null,                  -- 'adzuna' for now
    external_id text not null,             -- the source's own posting id
    title text not null,
    company text not null,
    location text,
    url text not null,                     -- the page the JD was actually
                                            -- captured from: Adzuna's
                                            -- redirect_url if tier 1/2
                                            -- succeeded (or nothing did and
                                            -- the snippet was kept), or the
                                            -- confirmed company careers-page
                                            -- URL if tier 3's fallback
                                            -- search succeeded -- tracks
                                            -- description_source (see below)
    description text,                      -- snippet until captured, full
                                            -- text after
    description_source text not null default 'adzuna_snippet'
        check (description_source in
            ('adzuna_snippet','redirect_url','company_site')),
    salary_min numeric,
    salary_max numeric,
    salary_is_predicted boolean,           -- see "salary precedence" in Phase 1
    work_location text                     -- 'remote' from Adzuna's own REMOTE
        check (work_location is null       -- badge when present (deterministic),
            or work_location in            -- else an LLM classification of the
                ('remote','hybrid','onsite')), -- JD text, else null if neither says
    match_category text
        check (match_category is null or match_category in ('strong','mixed','weak')),
    match_notes text,                      -- one-line reason from the categorization agent
    discovered_at timestamptz not null default now(),
    applied_at timestamptz,                -- set by the human, via Streamlit
    application_status text not null default 'not_applied'
        check (application_status in
            ('not_applied','applied','interviewing','rejected','offer','withdrawn')),
    unique (source, external_id)
);

create table company_research (
    company text primary key,
    culture_summary text,
    products_summary text,
    sources jsonb not null default '[]',   -- array of source URLs
    updated_at timestamptz not null default now()
);

create table agent_runs (
    id bigserial primary key,
    agent_name text not null,
    started_at timestamptz not null,
    finished_at timestamptz not null,
    status text not null check (status in ('success','partial','failed')),
    items_processed integer not null default 0,
    items_new integer not null default 0,
    error_message text
);

create table application_events (
    id bigserial primary key,
    posting_id uuid not null references postings(id),
    event_type text not null
        check (event_type in
            ('interview_scheduled','rejected','offer','followup','applied_confirmation')),
    detected_at timestamptz not null default now(),
    source_email_id text,
    notes text
);

create index idx_agent_runs_agent_name on agent_runs(agent_name, started_at desc);
```

Model these as Pydantic (v2) classes too, and treat the Pydantic models as
the contract every agent codes against -- the SQL is the storage detail.

Note `match_category` allows `'weak'` as a stored value, not just
`'strong'`/`'mixed'` — see "why there's no separate dedup ledger" below for
why, and see Phase 2 for the resulting rule that the dashboard and digest
must filter to `('strong','mixed')` rather than assuming every row is
worth showing a human.

### Why there's no separate dedup ledger (`seen_postings`)

An earlier version of this plan had discovery categorize fit on Adzuna's
thin snippet *first*, and only write a full row to `postings` for
strong/mixed survivors — under that design, a weak posting was never
recorded anywhere, so a separate ledger (`seen_postings`) was needed to
remember "I already rejected this one" and avoid re-processing it forever.
Building Phase 1 first changed the order: every result from the Adzuna
search gets fully captured and written to `postings` regardless of
eventual fit, *before* categorization exists at all. That removes the
reason for a separate ledger — `(source, external_id)` uniqueness on
`postings` itself already answers "have I processed this one before,"
since nothing is excluded from storage anymore. One ledger, not two.

### Why categorization happens once, after full capture, and isn't revisited

Phase 2's categorization step runs *after* Phase 1 has already captured
the full posting text — a meaningful improvement over judging fit from
Adzuna's thin snippet, which is what evaluating-before-capturing would
have meant. Categorization still only runs once per posting (on whichever
run first notices `match_category is null`) and is never recomputed
afterward — consistent with the general rule below that fit is a
per-posting, point-in-time judgment, not a recurring or comparative one.
If company research (also Phase 2) later surfaces something that would
have changed the read, that's useful information for the human reviewing
the dashboard, not something that triggers automatic re-categorization.

### Why fit categorization has no separate ranking agent

The categorization step assigns `strong` / `mixed` / `weak` to each
posting based on that posting alone against the resume — not compared to
other postings in the same run, and not a global re-ranking of historical
postings. There's no separate "ranking" agent; category is assigned once
and never recomputed. If ranking ever needs to become a recurring,
comparative pass later, that's a deliberate architecture change, not an
oversight to fix.

### Why Adzuna, not a per-company ATS list, is the discovery source

An earlier version of this plan started from Greenhouse's public Job Board
API. That API (and Lever's, Ashby's, Workday's) is per-company only --
there is no "search all customers for X" endpoint on any of them, because
none of them are built to answer that question. Given the actual
requirement is "find data scientist roles at companies I don't already
know about," a per-company API can't solve it no matter how many companies
get added to a watchlist -- you can't watch a company you don't know
exists. Adzuna is the option found that's both legitimate for this
(personal research is one of three uses explicitly permitted in its terms
of service) and actually keyword-searchable across many aggregated
sources. Its real cost is thinner per-posting data (snippet-only
descriptions, no guaranteed remote/hybrid field) -- which is what Phase
1's capture step exists to fill in, not a reason to avoid it.

Two Adzuna-specific things to carry into the implementation:
- **Attribution is a real ToS obligation, not a nice-to-have.** Adzuna's
  terms require crediting "The Adzuna API" with a link back, wherever the
  data is published -- this needs a line in the README and something
  visible on the Streamlit dashboard, not just a code comment.
- **Rate limits are tiered (25/min, 250/day, 1,000/week, 2,500/month), and
  the daily cap is the one that actually constrains a polling design** --
  each page of results is one hit, so budget hits per run accordingly (a
  single daily keyword search is typically 1-3 hits; this is not headroom
  for frequent/high-cadence polling against Adzuna specifically).

### Why `applied_at` is set by the human, not inferred by an agent

Only the person actually knows the moment they submitted an application on
a company's site -- an agent can't reliably infer that from anything
public. So "mark as applied" is a direct write from the Streamlit dashboard
(sets `applied_at` and flips `application_status` to `applied`), not an
agent action. The application tracker agent only ever operates on postings
where `applied_at is not null` -- it watches email for what happens *after*
a human-confirmed application, it doesn't try to detect the application
itself.

---

## Phase 1: Discovery and full posting capture

This is the part of the system with the most real-world uncertainty (can a
full posting reliably be recovered given only what Adzuna provides), so it
needs to be proven out before anything reasons over the result. Build only
what's described here — don't add scaffolding for Phase 2 "while you're in
there."

### What Phase 1 builds

1. An Adzuna search call, parameterized by keyword, location, minimum
   salary, and max posting age (`max_days_old`, defaulting to 14).
2. For each result, a check against `postings` on `(source, external_id)`
   -- if a row already exists, skip capture entirely for that result (see
   "Skip already-seen postings before capture" below). Otherwise, an
   attempt to capture the *full* posting (Adzuna only gives a snippet), in
   this order:
   - Try `redirect_url` directly.
   - If that fails — dead link, or a third-party portal that gates the
     real posting behind its own login (hackajob is a confirmed real
     example of this) — fall back to a general web search for the
     company's own careers page using the job title, then use whatever
     that page offers (browse, its own search box, filtering) to locate
     the specific role, cross-checked against Adzuna's `description`
     snippet and `location` to confirm it's the same posting and not just
     the company's careers page in general.
3. Extraction of salary (if present) and the full job description from
   whichever source succeeded, following the salary precedence rule below.
4. A write to the `postings` table (`match_category` left null), including
   the URL of whichever page the JD was actually captured from -- see "URL
   recorded" below.

### Explicitly out of scope for Phase 1

Fit categorization, company culture/product research, the application
tracker, the Slack digest, the Streamlit dashboard, the full
Langfuse/`agent_runs`/`STATUS.json` monitoring stack (basic error handling
and logging is fine for now — the full stack is worth the setup once this
is a scheduled job, not before), and dedicated Greenhouse/Lever/Ashby API
integrations (see below for why). All of these are Phase 2.

### 1. Adzuna search

`GET https://api.adzuna.com/v1/api/jobs/{country}/search/{page}` with
`app_id`, `app_key`, `what` (keyword), `where` (location), `salary_min`,
`max_days_old` (defaults to 14 -- keeps discovery focused on postings
still worth applying to, and keeps result volume, and therefore capture
cost, bounded), `title_only` (optional phrase restricted to matching the
posting's title specifically, on top of `what`'s full-text match), and
`full_time` (`1` to filter to full-time postings only, `0` for no filter --
defaults to `1`, confirmed to meaningfully change the result count in
practice). Built against the real observed response shape (see
`models/schema.py`'s `AdzunaResult`), not assumptions -- the exploration
script this was originally verified against has since been deleted, its
job done. Keep the attribution and rate-limit notes above in mind: this
should run once or a few times a day, not in a tight loop.

**`salary_min` must be serialized as a plain integer.** Adzuna's API
returns a 400 if `salary_min` is serialized with a decimal point (e.g.
`180000.0`) -- confirmed in practice, including via `requests` sending a
Python `float` exactly as `str()` renders it -- and accepts the identical
value serialized as an int (`180000`) with no error. `_search_params()` in
`mcp_servers/job_sources/server.py` casts `int(salary_min)` at the API
boundary so callers (CLI args, MCP tool inputs) can still pass a float
without needing to know this.

**Transient 5xx errors are retried, 4xx are not.** Confirmed in practice:
a `503 Service Temporarily Unavailable` on a completely ordinary request
(no bad params -- retrying the exact same request moments later returned
`200` three times in a row). `_get_with_retry()` in
`mcp_servers/job_sources/server.py` retries `{500,502,503,504}` up to 3
attempts total with linear backoff (1s, 2s), used by both `adzuna_search`
and `adzuna_count`. A 4xx is never retried -- that means something's
actually wrong with the request (e.g. the `salary_min` float-serialization
bug above), and retrying it would just burn API budget repeating the same
failure instead of surfacing it.

**Count without capturing.** Adzuna's response includes a `count` field --
the *true total* number of matching postings, independent of
`results_per_page` -- confirmed to still populate correctly even with
`results_per_page=0`, so getting it costs one cheap API hit with zero
job listings actually transferred. Exposed as its own MCP tool,
`adzuna_count`, separate from `adzuna_search` rather than piggybacked onto
it, since "how many matches" and "give me the postings" are genuinely
different operations with different costs. `discovery.py --count-only`
calls this and exits before touching the DB or the capture pipeline at
all -- useful for sanity-checking a query's volume before committing to a
real (costed) run against it.

**List without capturing.** `discovery.py --list-only` runs the same
paginated search as the normal path (see "Pagination" below) and logs
each result's title and company, then exits -- skipping the dedup check,
tier 1-3 capture, and the DB write entirely. No new MCP tool needed here
(unlike `--count-only`): it's the same search `discovery.py`'s normal
path already makes, just stopping before the per-posting loop that would
otherwise capture and write each one. Useful for eyeballing what a query
actually returns before spending real capture cost on it.

**Pagination.** A single `adzuna_search` call only ever returns one page
(bounded by `results_per_page`, `page` defaulting to 1) -- there's no "give
me everything" mode on the API itself. `discovery.py`'s `fetch_all_pages()`
loops pages until either a page comes back shorter than
`--results-per-page` (the actual last page) or `--max-pages` (default 5)
is hit, whichever comes first. `--max-pages` exists specifically because
each page is one rate-limited Adzuna hit against a 250/day cap (see above)
-- an unbounded "fetch every page" loop on a broad query (tens of
thousands of matches) would blow through that in a single run. Coverage is
never silently capped: `fetch_all_pages()` calls `adzuna_count()` first
(one cheap extra hit) and logs explicitly when `--max-pages` means some
matches weren't fetched, e.g. "Covered 15 of 23 ... 8 beyond
--max-pages=3 were not fetched" -- same "no silent caps" pattern as tier
3's candidate walk in `fetchers.py`. For an eventual scheduled/automated
run: narrow the query (`--title-only`, a tight `--max-days-old`) so the
true match count stays small enough that `--max-pages` covers it
completely, rather than relying on a large `--max-pages` to brute-force
coverage of a broad query -- narrowing the query is what keeps daily API
hits and per-posting capture cost bounded, pagination alone doesn't.

**`--count-only` checks capacity too, not just the total.** Since it's the
natural pre-flight check before a real run (and especially before a
scheduled one -- confirm the query stays narrow before automating it),
`_log_capacity_warning()` compares the count against
`--results-per-page x --max-pages` and logs explicitly when the *current
settings* wouldn't cover it -- this is an estimate (no pages are actually
fetched in `--count-only` mode), cross-checked for real once
`fetch_all_pages()` actually walks pages during a real run. Confirmed in
practice: silent for a narrow, single-day query (16 matches vs. 100
capacity), logs a clear warning for a broad one (1,845 vs. 100).

### Skip already-seen postings before capture

Before attempting capture on an Adzuna result, check `postings` for an
existing row with the same `(source, external_id)`. If one exists, skip
capture for that result entirely -- it's already been captured (or
degraded to a snippet) on a previous run, and re-running tiers 1-3 against
it would just repeat the same web fetches/searches/LLM calls for no new
data. This is purely a cost/traffic optimization on top of the uniqueness
constraint described in "why there's no separate dedup ledger" above --
that constraint already guarantees no duplicate row could ever be written;
this check is what stops the (expensive) capture flow from running at all
for postings already on file. Only genuinely new `external_id`s reach the
tier 1-3 capture flow below.

### 2. Full posting capture

Tier 3 is built as fetch tiers with an LLM doing the judgment calls, not
hand-written parsing — hand-rolled HTML selectors or string-matching will
break across the wide variety of *external* site structures tier 3 hits,
and "is this the same posting" / "extract the salary and description from
this page" are exactly the kind of fuzzy judgment an LLM handles well and
brittle heuristics don't. Tier 1/2 is the deliberate exception to that
rule: it's a single, known, stable target (Adzuna's own page), not an
arbitrary site, so a targeted structural extraction there is a legitimate,
bounded case rather than the kind of brittle guesswork this principle
warns against — see the JSON-LD bullet under tier 1 below for why that
distinction holds and what was verified before relying on it.

- **Tier 1 — redirect_url, plain fetch.** Cheap, works for a meaningful
  chunk of postings (server-rendered sites, or a redirect that already
  lands on a plain-text-ish page).
  - **Adzuna URL normalization, applied before the fetch.** Adzuna's own
    `redirect_url` often comes back as
    `adzuna.com/land/ad/{id}?{query}` -- a landing/tracking page with real
    bot-protection that blocks both plain fetch (403) and tier 2's headless
    render ("Access Denied... suspicious behaviour"), confirmed in practice
    on multiple postings, including ones that would otherwise need the full
    tier 3 search. Adzuna also serves the exact same posting directly at
    `adzuna.com/details/{id}?{query}` -- same query string, no gate --
    confirmed in practice across several postings, including ones tier 1/2
    couldn't otherwise touch at all. So: rewrite `land/ad` to `details` in
    the URL (regex on the path only, query string untouched) before
    attempting tier 1, and use the result -- not the original -- as `url`
    if capture succeeds via this path. Any URL not matching that exact
    pattern (including every tier 3 candidate, which is never adzuna.com in
    the first place) passes through unchanged.
  - **Tier 1/2's extraction is deterministic, not LLM-judged.** Adzuna's
    `/details/{id}` page (after normalization) embeds a `JobPosting`
    JSON-LD block with a clean, already-isolated `description` field --
    confirmed by fetching real posting pages directly and inspecting the
    raw HTML, not assumed. `_extract_metadata_text()` (originally built for
    Workday's JS-shell case) already pulls this out; `capture()` uses its
    presence, unmerged with anything else, directly as `full_description`
    for tier 1/2 -- no `_confirm_and_extract`/LLM call at all on this path.
    Its *absence* doubles as the validity gate that an LLM `gated` judgment
    used to provide: confirmed in practice on a posting that expired
    between two points in this same investigation -- the *visible* page
    still looked like a plausible, current listing, but the `JobPosting`
    JSON-LD node was gone. That's a more reliable signal than judging
    visible text would have been, not just a cheaper one. No salary
    detection happens on this path either (see "Salary detection is tier
    3-only" below) and no identity check happens either, since there's no
    "which posting is this" ambiguity on a page Adzuna serves directly by
    this exact posting's own ID, only whether the JobPosting node exists at
    all. If it's absent or too short, `capture()` falls through to tier 3
    exactly as if tier 1/2 had failed any other way.
- **Tier 2 — redirect_url, headless render (Playwright/Chromium)** if the
  plain fetch's text is suspiciously short. A lot of modern career sites
  only populate the description via client-side JavaScript that a plain
  fetch never executes.
- **Tier 3 — fallback search**, triggered when tiers 1–2 produce unusable
  text *or* when the page is clearly a login/paywall (e.g. a login form
  where content should be, an auth redirect, a "sign in to view" message —
  worth having the LLM judge this directly from the fetched content rather
  than hardcoding site-specific detection). Two separate steps, not one
  agentic loop doing both:
  - **Search** — a single, plain web search for `"{title} {company}"`, the
    same kind of query a person would type by hand. Deliberately *not*
    "{company} careers" plus browsing around the careers site, and
    deliberately one query, not a sharpened retry: the Adzuna snippet is
    generic opening boilerplate with nothing distinctive enough to sharpen
    a second query with anyway. This step is mechanical retrieval — a
    ranked list of result URLs — not a judgment call, so it must be a
    single constrained tool call (search only, no fetching/browsing/
    evaluating), not a multi-turn agentic loop.
  - **Candidate walk** — fetch each result URL, in ranking order, through
    the *same* tiered fetch used for `redirect_url` (tier 1 plain fetch,
    escalating to tier 2's headless render if the text looks too thin).
    Stop at the first candidate whose page passes the confirmation step
    below. This reuses tier 1/2's fetch logic and the confirmation step
    rather than duplicating either — in particular, the model must never be
    asked to reproduce a page's full text as structured output; letting it
    verbatim-echo a JD as output tokens is expensive and unreliable
    (fetching inside an agentic tool call also can't render JavaScript, so
    a client-rendered career site just comes back as an empty loading
    shell regardless of how good the search was). If no candidate passes,
    this degrades the same way tier 1/2 failing does — keep the Adzuna
    snippet, don't block the pipeline.
  - **Candidate cap** (`MAX_FALLBACK_CANDIDATES`, currently 10 — raised
    from an initial 5, see "Cost controls" below for why) — walking
    unboundedly many search results would mean unboundedly many
    fetch+confirm calls per posting; capped for the same cost reasons as
    the rest of this section.
- **Confirmation and extraction, in a single LLM call -- tier 3 only.**
  Once a tier 3 candidate page is found, one call judges whether it's
  actually the same posting Adzuna described (comparing against Adzuna's
  title, description snippet, and location — careers pages often list
  many roles, so landing on the page isn't the same as landing on the
  right posting) *and*, if so, extracts the full job description plus
  whether the JD itself states a salary or range — as an explicit
  found/not-found signal the call returns, not something inferred later
  from whether a number happens to come back non-null. This started as
  two separate calls (confirm, then extract) and was deliberately merged
  into one: both operate on the same page text, so splitting them only
  doubled the LLM round-trips for no benefit — the model is told to leave
  the extraction fields empty/false when the page fails confirmation
  rather than guess. Revisit this if Phase 2's categorization step ends up
  wanting a from-scratch look at whether merging trades away too much
  per-step clarity — noted as a live open question, not a settled
  non-issue. This step doesn't run at all for tier 1/2 -- see the JSON-LD
  bullet under tier 1 above, which replaced it (identity check included:
  a search result or external careers page can genuinely show any of
  several roles, which is exactly the ambiguity Adzuna's own ID-keyed page
  doesn't have).
- **Page text includes metadata, not just what's visibly rendered -- and
  this is now doing double duty.** Some career sites (Workday-hosted ones,
  confirmed in practice) embed the full JD in a `JobPosting` JSON-LD block
  server-side for SEO, even though the visible page is a JS-rendered shell
  until the client app loads. Originally built for tier 3 candidates on
  those sites (pulling this in alongside the rendered visible text meant
  tier 1's plain fetch could pick up the real JD without needing tier 2's
  render), the exact same mechanism turned out to be what Adzuna's own
  `/details/{id}` page uses too -- which is what tier 1/2's deterministic
  extraction (above) is actually built on top of. Not a coincidence worth
  re-deriving if this code is touched again: `_extract_metadata_text()` is
  one function serving both call sites.
- **Visible text is main-content-extracted, not just script/style-stripped.**
  `trafilatura` (general content-density heuristics, not site-specific
  rules -- same "don't hardcode site structure" reasoning as everywhere
  else in this section) drops nav/menus/cookie banners/footers/"related
  jobs" widgets before the LLM ever sees the text. Confirmed in practice: a
  ~34% cost reduction on a real posting, comparing this against the old
  whole-page-text approach on identical input, via directly measured
  `total_cost_usd` -- not just a smaller character count (which doesn't
  reliably predict cost; on at least one real page trafilatura's output
  was *longer* than the naive approach, because it restructures list-style
  metadata that flat-text extraction mangles, and it still costs less to
  process). Falls back to the old whole-page approach when trafilatura
  finds no extractable "main content" at all (e.g. a near-empty JS-shell
  page) so nothing is silently lost -- confirmed this still recovers
  correctly on the Workday-shell case above, since that case's real
  content comes from the JSON-LD metadata pass either way.
- **Graceful degrade:** if nothing above works, keep Adzuna's snippet
  (`description_source = 'adzuna_snippet'`) rather than blocking on a page
  that won't cooperate. Let the human glance at the original
  `redirect_url` themselves in the rare case it's worth it.

**Cost controls on the LLM calls.** An early Phase 1 test run burned ~5% of
a $25 API budget on a single posting before these existed -- tier 3's
original design was a single multi-turn agentic loop doing search, browsing,
fetching, and full-page-text reproduction all in one call, and left
unbounded on model choice/turns/spend, that got expensive fast, especially
since a meaningful fraction of postings hit tier 3 in practice (Adzuna's own
`/land/ad/` redirect pages frequently don't resolve to the real posting on a
plain fetch). Splitting tier 3 into a plain single-query search (mechanical
retrieval) plus a candidate walk over the *existing* tiered fetch and
confirmation logic (see tier 3 above) removes most of that cost by
construction -- no LLM call ever reproduces a full page's text as output
tokens anymore. The biggest single reduction came later, though: **tier
1/2 makes zero LLM calls at all now** (see the JSON-LD deterministic
extraction under tier 1 above) -- confirmed directly (not estimated) by
running a real capture end-to-end and reading `get_total_cost_usd()`
before and after: `$0.00` for a posting that resolved via tier 1/2, versus
real Haiku cost on the same posting before that change. Every posting
that resolves via tier 1/2 -- the common case, especially post-URL-
normalization -- now costs nothing in LLM spend at all; only tier 3
candidates still make LLM calls. Confirm and extract are also merged into
one call rather than two on that remaining tier 3 path (see "Confirmation
and extraction" above), halving the LLM round-trips for every candidate
that reaches it. The remaining LLM calls (tier 3's confirm+extract, tier
3's search) still all set:
- **Model: Haiku** (currently `claude-haiku-4-5-20251001`), not whatever
  the CLI defaults to. These are narrow judgment/lookup tasks -- same-page
  confirmation, text extraction, reporting back search result URLs -- not
  tasks that need a frontier model's reasoning.
- **A hard per-call budget cap** (`max_budget_usd`, currently `0.15`) so
  one runaway call can't blow through real money. The SDK stops the query
  and returns an `error_max_budget_usd` result when hit -- treat that
  exactly like any other failed capture attempt (graceful degrade, not a
  crash).
- **A capped turn budget on tier 3's search call** (`max_turns`, currently
  `6`) -- confirm+extract is single-turn and doesn't need this. The search
  call needs more headroom than "one tool call plus a final answer" implies:
  `WebSearch` is a *deferred* tool in this harness, so the model has to call
  `ToolSearch` to fetch its schema before it can invoke it, then the actual
  search, then the structured-output call -- 3 turns with zero margin,
  confirmed in practice to silently truncate (`is_error=True`, no result,
  zero candidates returned) the moment anything needed a 4th.
- **A wall-clock timeout per call** (currently 120s) so a stalled query
  degrades gracefully instead of hanging the whole discovery run.
- **A cap on how many tier 3 candidates get walked** (`MAX_FALLBACK_CANDIDATES`,
  currently 10 -- raised from an initial 5 after confirming in practice
  that the WebSearch tool's ranking can put the real posting outside the
  top 5) -- see tier 3 above.

Before scaling up `results_per_page` on a real run, smoke-test with a
small page size first and sanity-check actual spend -- these caps bound
the *worst case* per call, not the total cost of a large run. To make that
sanity-check concrete rather than a guess: every Claude call's
`ResultMessage.total_cost_usd` is accumulated into a running total
(`agents/fetchers.py`'s `get_total_cost_usd()`), and `discovery.py` logs it
after every posting and again as a final total when the run completes --
actual spend for a run is always visible in the log, not just the
per-call worst-case bound.

**URL recorded.** `url` stores whichever page the JD was actually captured
from, not just Adzuna's original link -- this tracks `description_source`:
- `description_source = 'redirect_url'` (tier 1/2 succeeded) -- `url` is
  whatever tier 1/2 actually fetched from, which is the *normalized* URL
  when Adzuna URL normalization (see tier 1 above) applied, not necessarily
  the raw `redirect_url` Adzuna returned.
- `description_source = 'adzuna_snippet'` (nothing succeeded) -- `url`
  stays Adzuna's original `redirect_url`, unnormalized, since that's the
  best link available to hand the human in this case (not necessarily what
  was actually attempted).
- `description_source = 'company_site'` (tier 3's fallback search
  succeeded) -- `url` is overwritten with the confirmed company
  careers-page URL the JD was actually pulled from, not the original
  `redirect_url`.

**Salary precedence.** If the JD extraction found a stated salary, use it:
`salary_min` / `salary_max` come from the JD, and `salary_is_predicted =
false` — a number the actual posting states outright is definitionally
not a prediction. If the JD didn't state one, fall back to Adzuna's
`salary_min` / `salary_max` *and* whatever Adzuna's own
`salary_is_predicted` flag already says, unchanged. This makes
`salary_is_predicted` do double duty — "is this number trustworthy" —
directly, rather than needing a separate provenance column. Edge case
worth handling explicitly: if the JD states a single figure rather than a
range (e.g. "$150k"), treat that as `salary_min = salary_max` = that
figure.

**Salary detection is tier 3-only, not tier 1/2.** Tier 1/2's deterministic
extraction (see tier 1 above) has no salary handling at all -- only
`_confirm_and_extract()` (tier 3 candidates) does. Reasoning: tier 1/2's page
*is* Adzuna's own
data source (`adzuna.com/details/{id}`) -- there's no independent JD there
to check against Adzuna's own salary fields, since Adzuna's own
`salary_min`/`salary_max`/`salary_is_predicted` already reflect whatever
that exact page shows. A tier 3 candidate is a genuinely different source
(an external company site Adzuna never computed its own salary data from),
so checking its JD for a stated salary is checking something new, not
re-deriving what the API already gives for free. `capture()`'s salary
block reads `extraction.get("salary_found")` (not bracket access)
specifically so a tier 1/2 result -- which has no such key -- falls
straight through to Adzuna's own salary fields rather than crashing.

**Remote/hybrid/onsite detection (`work_location`).** Three-way field
(`'remote' | 'hybrid' | 'onsite' | null`), not boolean -- deliberately
widened from an earlier `is_remote` boolean once hybrid turned out to
matter for real (see below). Two deterministic things had to be verified
before building any of this, not assumed:
- **Adzuna's REMOTE badge doesn't survive `trafilatura` cleaning** --
  confirmed directly (see "Visible text is main-content-extracted" above):
  a short, isolated UI badge is exactly what content-density extraction
  treats as chrome and discards, the same as the salary widget. So this
  has to be pulled from the *raw* HTML before cleaning runs, via
  `_detect_remote_badge()` in `_extract_text()` -- the same pattern as the
  JSON-LD metadata pass, not a new architectural idea. Anchored on the
  badge's literal text (`REMOTE`), not its Tailwind CSS classes, which are
  purely cosmetic and fragile to key off.
- **No equivalent badge exists for hybrid or onsite roles** -- confirmed:
  nothing to key off there. So the badge alone only ever answers "remote"
  or "no signal," never "hybrid" or "onsite" outright.
- Also confirmed: Adzuna's separate *salary* widget (`ui-salary` div) is
  **not** worth extracting the same way, deterministic or otherwise -- it's
  just a rendering of the same `salary_min`/`salary_max`/`salary_is_predicted`
  the API already returns for free, confirmed by its own UI text literally
  saying "estimated" when `salary_is_predicted` would be true. No new
  information there; only the badge was worth this treatment.

**Deterministic badge first; a minimal, output-bounded LLM call as
fallback -- on *both* tiers, not just tier 3.** When the badge is found,
`work_location = "remote"` outright, no LLM call needed. When it's
absent:
- **Tier 3** already makes an LLM call for confirm+extract, so
  `work_location` is just an added field on that same call (enum
  `remote`/`hybrid`/`onsite`/`unknown`) -- no extra round-trip.
- **Tier 1/2** makes *no other* LLM call (see the JSON-LD deterministic
  extraction under tier 1 above) -- this was a deliberate exception added
  back on top of that: `_classify_work_location()`, a single call whose
  *entire* output is one enum word, invoked only when the badge didn't
  already answer it. This was cut once already (badge-only, no fallback)
  and reinstated because losing hybrid/remote signal stated only in JD
  prose was judged worse than the added cost -- see the cost investigation
  immediately below before assuming this call is "basically free" because
  its output is tiny.

**Cost reality check, measured directly, not assumed from output size.**
The intuition "tiny output -> tiny cost" doesn't hold here. Confirmed
directly via raw `ResultMessage.usage`: each fresh `query()` call pays a
roughly fixed ~$0.03 overhead dominated by `cache_creation_input_tokens`
(the system prompt + tool list + our JSON schema, billed as a cache
*write* every time) -- and this does **not** shrink on repeated calls with
an identical schema+prompt shape within the same process, confirmed by
running three back-to-back classification calls and seeing
`cache_creation_input_tokens` stay ~16,000 on all three rather than
dropping after the first (no cross-call cache reuse the way a persistent
conversation would get). So `_classify_work_location()`'s real cost is
~$0.03–0.06 per call depending on JD length, not near-zero -- barely
cheaper than a full confirm+extract in some cases. The actual saving comes
from call *avoidance* (most postings resolve via the badge and cost
$0.00), not from this call being intrinsically cheap when it does run.

**A cheap keyword pre-filter for this call was investigated and rejected --
concrete counter-example, not just a theoretical concern.** Idea: pass the
classifier only sentences containing "remote"/"hybrid"/"onsite" instead of
the full JD, to shrink input size. Tested directly on a real posting: the
keyword filter matched *zero* sentences, yet the full-text call correctly
returned `work_location = "onsite"` for it -- the actual signal was
"Priority will be given to candidates who live in the Columbus, OH
metropolitan area," an inference from residency-preference language, not
any literal use of the three keywords. Real postings signal work
arrangement obliquely all the time (relocation requirements, "based in
the tri-state area," in-person-collaboration language) without ever using
those words -- exactly the kind of fuzzy judgment the LLM call exists for
in the first place. A keyword filter would silently downgrade real
classifications to `unknown` in cases like this, so the full JD text is
passed to `_classify_work_location()`, not a pre-filtered subset.

Absence of the badge is *not* treated as "confirmed onsite" on either
tier -- it's "no deterministic signal," and the LLM fallback (when it
runs) can still answer any of remote/hybrid/onsite/unknown.

A real, expected failure mode worth planning for: some fraction of
postings simply won't resolve cleanly (recruiter postings that don't
disclose the employer, a company name that doesn't match its careers-page
brand, sites that block automated access outright). That's a normal
outcome to degrade gracefully from, not a bug to eliminate entirely.

Keep this fetch/capture flow architecturally independent from the Adzuna
call that produced the posting in the first place -- i.e. don't build it
as "extract data from Adzuna's response to reach a third party," build it
as "given a company name and title (plain facts), independently look for
their public posting." This distinction matters given Adzuna's terms of
service require directing queries through Adzuna and prohibit contacting
third-party content providers reached via Adzuna. This reading isn't a
legal guarantee, just the most sensible interpretation available; keeping
the two flows separate is the safer implementation regardless of how that
clause is ultimately interpreted.

### Why no dedicated Greenhouse/Lever/Ashby tools in Phase 1

If a company's careers page happens to be hosted on one of these
platforms, it's reached and handled exactly like any other company
website — general search finds it, the same fetch/Playwright tiers read
it. There's no detection branch and no specialized API client. This
trades away a possible efficiency gain (those platforms' own APIs return
clean structured JSON instead of rendered HTML) for a meaningfully simpler
build: one fetch/search/extract pipeline instead of one general path plus
three platform-specific ones. If it turns out a large share of postings
land on `boards.greenhouse.io`-style URLs, that's a cheap, narrow
optimization to add later — detect the URL pattern, swap to the
structured API for just that case — not a reason to build it upfront.

### Suggested files for Phase 1

```
job-hunt-agents/
├── db/
│   ├── schema.sql           # full schema above
│   └── db.py
├── models/
│   └── schema.py            # Pydantic models; match_category: Optional
├── mcp_servers/
│   └── job_sources/
│       └── server.py        # Adzuna search tool only, for now
├── agents/
│   ├── discovery.py         # Adzuna search -> writes raw postings (no categorization)
│   └── fetchers.py          # tier 1/2/3 capture logic + LLM confirm/extract calls
├── .env.example             # ANTHROPIC_API_KEY, ADZUNA_APP_ID, ADZUNA_APP_KEY, DATABASE_URL
└── pyproject.toml            # uv-managed; see the setup note below
```

**Setup note confirmed necessary in practice:** `uv add playwright` (or any
`pip install playwright`) only installs the Python package -- it does not
install the actual browser binary tier 2 launches. Without a separate
`uv run playwright install chromium` (one-time, downloads Chromium), tier 2
fails for *every* posting that needs it, silently -- `_fetch_rendered()`
catches the "executable doesn't exist" error the same as any other tier 2
failure and just falls through to tier 3, so this is easy to miss without
reading the logs closely. Needed once per machine/environment this runs on
(local dev and CI both), not just once per clone.

---

## Phase 2: Categorization, research, tracking, and delivery

Builds on Phase 1's output — reads postings Phase 1 already captured,
doesn't redo discovery or capture.

### What Phase 2 builds

- **Fit categorization.** For every posting where `match_category is
  null`, judge `strong` / `mixed` / `weak` against the resume using the
  full captured description — see "why categorization happens once, after
  full capture" above. All three outcomes get written back to the same
  row (no deletion, no separate table — see "why there's no separate dedup
  ledger" above); only `strong`/`mixed` are ever surfaced to the human.
  **Resume ingestion:** the resume is a plain markdown file, kept outside
  version control (`resume/` is gitignored -- personal data), loaded via
  `agents/resume.py`'s `load_resume()`, which reads whatever path
  `RESUME_PATH` (in `.env`) points to. Same pattern as every other
  required config in this project (a `.env` var, required, no silent
  fallback) rather than a DB table or a hardcoded path -- there's exactly
  one resume, it changes rarely, and it shouldn't live in the repo at all
  given what it contains.
- **Company research**, for strong/mixed postings only: culture and
  product summary via web search, written to `company_research` along
  with the source URLs used.
- **Application tracker**: reads email (Gmail, read-only scope) for
  postings where `applied_at is not null` (set by the human via
  Streamlit — see rationale above), and updates `application_status` plus
  logs to `application_events` as things happen (interview scheduled,
  rejected, offer, etc).
- **Slack digest** of newly strong/mixed postings after each run.
- **Streamlit dashboard**: a "new matches" tab (`match_category in
  ('strong','mixed')` and not yet applied to, with a mark-as-applied
  action and inline company research), a "pipeline" tab (applied
  postings, current status, event history), and a "pipeline health" tab
  (see monitoring below).
- **Full observability**: Langfuse tracing, the `agent_runs` table, and
  the self-committing `STATUS.json` (see "Monitoring and observability"
  below).
- **Scheduling**: the GitHub Actions workflow that runs the whole pipeline
  (see "Scheduling and triggering" below).

### Suggested files for Phase 2

```
job-hunt-agents/
├── README.md
├── docker-compose.yml
├── Dockerfile
├── STATUS.json                  # committed by CI, not gitignored
├── observability/
│   └── tracing.py               # Langfuse client + environment config
├── agents/
│   ├── common.py                # AgentRunTracker: agent_runs + STATUS.json
│   ├── categorize.py            # fit categorization, reads match_category IS NULL
│   ├── research.py              # company culture/product research only
│   └── tracker.py               # Gmail-based, read-only scope
├── digest/
│   └── send_digest.py
├── dashboard/
│   └── app.py                    # Streamlit: new matches / pipeline / health
├── scripts/
│   └── run_pipeline.py           # entrypoint: discovery -> categorize -> research -> tracker -> digest
└── .github/
    └── workflows/
        └── agents.yml            # schedule + workflow_dispatch, STATUS.json commit step
```

---

## Sourcing strategy

**Discovery: Adzuna Search API**
(`https://api.adzuna.com/v1/api/jobs/{country}/search/{page}`), queried by
title keyword, capped to postings at most `max_days_old` old (default 14).
Returns title, company, location, category, salary_min/max (with a
`salary_is_predicted` flag), a `created` timestamp, a snippet description,
and a `redirect_url`. See "Why Adzuna, not a per-company ATS list" above.

**Full posting capture** is Phase 1's job in full — see that section.
There is no separate "verification via ATS APIs" step; general web search
covers ATS-hosted careers pages the same as any other company site.

**Deliberately ruled out, with reasons, so these don't get re-proposed:**
- Indeed -- Publisher API and XML feed both retired (2023/2024); no
  self-serve way to read postings anymore, only employer-side partner APIs.
- LinkedIn -- no public read API for third parties; scraping it carries
  real legal risk (see the Proxycurl/LinkedIn litigation precedent).
- ZipRecruiter -- current partner docs are entirely employer-side (posting
  jobs in); no maintained public search/read API found.
- hiring.cafe -- no official developer API; what exists is third-party
  scrapers of its internal, undocumented API, which is the same
  ToS-risk pattern as LinkedIn scraping, just one layer removed.
- MyGreenhouse Jobs -- a real cross-company search feature inside
  Greenhouse, but opt-in per employer (not all Greenhouse customers
  participate) and no documented public API was found for it, only a
  signed-in candidate portal UI.
- Third-party ATS-aggregator vendors (JobsPipe-style) -- legitimate
  product, but using one swaps "I sourced this cleanly myself" for "I
  depend on a vendor's scraping being fine," and costs money past a small
  free tier.

## Scheduling and triggering

No true webhooks exist for this: webhook support on Greenhouse/Lever/Ashby
is an employer-side ATS feature, configured from inside the *hiring
company's* own account -- not something available to an outside job board
consumer. Anything marketed as a "new posting webhook" from a third-party
vendor is that vendor polling on your behalf and re-emitting a
notification; the underlying mechanism is still polling.

So: GitHub Actions on a `schedule` trigger (plus `workflow_dispatch` for
manual runs), polling Adzuna for discovery and using general web
search/fetch for capture. This is not a lesser architecture than a
webhook -- it's the same thing everyone else in this space actually does,
just self-hosted for free instead of paid for through a vendor.

Known GitHub Actions quirks to build around:
- `schedule` is documented as best-effort with no timing SLA.
- Scheduled workflows on public repos auto-disable after 60 days with no
  commits to the repo (not "no runs" -- no *commits*). See the STATUS.json
  step below, which solves this as a side effect.

This whole section is Phase 2 scope — Phase 1 is run manually while its
logic is validated, not on a schedule yet.

## Monitoring and observability

Phase 2 scope. Layered, using infrastructure already in this plan rather
than new tools:

1. GitHub's built-in failure email (opt in, in notification settings) --
   catches "the workflow crashed."
2. A **self-committing STATUS.json** written by every agent run (success or
   failure) and committed back to the repo by the workflow as its last
   step. This does double duty: it's a human-readable "last run" status
   visible right in the repo, and the commit itself resets the 60-day
   inactivity clock -- no separate keepalive action needed.
3. The `agent_runs` table -- one row per agent execution (status, item
   counts, error message), written from a shared context-manager helper so
   every agent logs consistently. This is what the Streamlit "pipeline
   health" tab reads.
4. Langfuse -- already instrumenting every LLM call, so malformed output,
   rate limits, and per-call cost are visible automatically. Use Langfuse's
   native `environment` tag (`development` locally, `production` in CI) to
   keep local test runs visually separate from real ones without a second
   Langfuse project.

A Slack failure notification (`if: failure()` step, webhook curl) is a
reasonable addition once the above exists, not before.

## Environments

Single hosted environment, not local Docker containers for the database:
Neon Postgres (pgvector extension available, not necessarily used) and
Langfuse Cloud, used both locally and from GitHub Actions. Reasoning: this
app accumulates one continuous, ongoing record of a real job search -- a
posting captured on Tuesday needs to still be there Thursday when
categorization or the tracker looks at it -- so local and scheduled runs
writing to genuinely different databases doesn't make sense here the way
it might for a stateless service. This applies starting in Phase 1 —
Neon needs to be set up before the first discovery run, it isn't a
Phase-2-only concern.

For safe local iteration without touching real data: cut a Neon branch
(e.g. `dev`) and point local runs at it via `DATABASE_URL`; point CI
(GitHub Actions secrets, added in Phase 2) at the `main` branch. Same idea
for Langfuse via the `environment` tag mentioned above, once Langfuse is
added in Phase 2.

`docker-compose.yml` (Phase 2) only packages the app runtime (the pipeline
script, the dashboard) -- it does not run Postgres or Langfuse locally.

## Tools and stack

Needed from Phase 1:
- Python throughout.
- Claude Agent SDK (`claude-agent-sdk`) for the LLM-driven judgment calls
  (capture confirmation/extraction now; categorization and research in
  Phase 2) -- verify current `query()` / `ClaudeAgentOptions` API and the
  `mcp_servers` config shape against the live docs when implementing; this
  evolves and shouldn't be assumed from memory. See "Cost controls on the
  LLM calls" in Phase 1 -- Haiku, a per-call budget cap, and a turn limit
  are not optional extras, they're load-bearing for this being affordable
  to run at all.
- A custom MCP server (FastMCP) exposing Adzuna search as a tool. No
  dedicated ATS lookup tools -- see Phase 1 for why.
- `requests` (or `httpx`) for the Adzuna calls.
- Playwright (Python), Chromium only, for the capture fallback tier.
- `trafilatura` for main-content HTML extraction before any page text
  reaches an LLM call -- see "Full posting capture" for the confirmed
  cost impact.
- Pydantic v2 for every shared data model.
- `psycopg` (v3) for Postgres access.

Added in Phase 2:
- Langfuse Python SDK, `@observe` decorator, `get_client()`.
- Streamlit for the dashboard (three tabs, see Phase 2 above). Deploy
  privately (Streamlit Community Cloud's free tier includes one private
  app) since this shows real resume/application data.
- Slack incoming webhook for the digest, falls back to stdout if
  `SLACK_WEBHOOK_URL` isn't set.
- Docker + docker-compose for the app runtime (see Environments above).
- GitHub Actions for scheduling (see Scheduling above).

## Environment variables / secrets

```
# Needed from Phase 1
ANTHROPIC_API_KEY=
ADZUNA_APP_ID=
ADZUNA_APP_KEY=
DATABASE_URL=                      # Neon connection string (main branch for CI)

# Added in Phase 2
RESUME_PATH=                       # path to a plain markdown resume file,
                                    # kept outside version control (see
                                    # "Fit categorization" above) -- wired
                                    # up ahead of the rest of Phase 2, since
                                    # categorize.py needs it from day one
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
LANGFUSE_HOST=https://cloud.langfuse.com
LANGFUSE_TRACING_ENVIRONMENT=      # development locally, production in CI
SLACK_WEBHOOK_URL=
GMAIL_CREDENTIALS_JSON=            # read-only Gmail scope only
```

GitHub Actions (Phase 2) needs the same set as repository secrets, plus
the workflow needs `permissions: contents: write` to commit STATUS.json.

## Suggested build order

**Phase 1:**
1. `db/schema.sql` + `models/schema.py` -- get the contract right first.
2. `mcp_servers/job_sources/server.py` -- the Adzuna search tool, tested
   standalone (verified against a throwaway exploration script for the
   actual response shape before wiring it into MCP; since deleted, its
   job done -- see `models/schema.py`'s `AdzunaResult` instead).
3. `agents/discovery.py` -- Adzuna search, writing raw rows.
4. `agents/fetchers.py` (plain fetch -> Playwright -> fallback search ->
   degrade) wired into discovery so every written row has its best-effort
   captured description and salary.

**Once Phase 1 is validated against real postings, Phase 2:**
5. `agents/common.py` (`AgentRunTracker`) -- every Phase 2 agent depends
   on this.
6. `agents/categorize.py` -- fit judgment against postings with
   `match_category is null`.
7. `agents/research.py` (company culture/product context for
   strong/mixed postings).
8. `dashboard/app.py` -- once there's categorized data to look at,
   iterating on the rest gets much easier.
9. `agents/tracker.py` (can stay a stub with clear TODOs for the Gmail
   OAuth setup -- that's a one-time interactive step, not something to
   automate away).
10. `digest/send_digest.py`.
11. `.github/workflows/agents.yml`, wired to the secrets above, with the
    STATUS.json commit step.
12. `docker-compose.yml` + `Dockerfile` + README last, once the app
    actually runs, so setup instructions are accurate rather than
    aspirational.
