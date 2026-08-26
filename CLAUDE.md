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
cost, bounded). See the throwaway `explore_adzuna.py` script already run
against this account for the actual response shape — build against real
observed fields, not assumptions. Keep the attribution and rate-limit
notes above in mind: this should run once or a few times a day, not in a
tight loop.

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

Build this as fetch tiers with an LLM doing the judgment calls, not
hand-written parsing — hand-rolled HTML selectors or string-matching will
break across the wide variety of site structures this will hit, and
"is this the same posting" / "extract the salary and description from this
page" are exactly the kind of fuzzy judgment an LLM handles well and
brittle heuristics don't.

- **Tier 1 — redirect_url, plain fetch.** Cheap, works for a meaningful
  chunk of postings (server-rendered sites, or a redirect that already
  lands on a plain-text-ish page).
- **Tier 2 — redirect_url, headless render (Playwright/Chromium)** if the
  plain fetch's text is suspiciously short. A lot of modern career sites
  only populate the description via client-side JavaScript that a plain
  fetch never executes.
- **Tier 3 — fallback search**, triggered when tiers 1–2 produce unusable
  text *or* when the page is clearly a login/paywall (e.g. a login form
  where content should be, an auth redirect, a "sign in to view" message —
  worth having the LLM judge this directly from the fetched content rather
  than hardcoding site-specific detection). Search for the company's
  careers page (e.g. "{company} careers" or "{company} jobs"), fetch/render
  it the same two-tier way, and if it has its own listing or search
  interface, use it to get to the specific role.
- **Confirmation step:** once a candidate page is found (tier 1, 2, or 3),
  have the agent judge whether it's actually the same posting Adzuna
  described — compare against Adzuna's title, description snippet, and
  location — before trusting its extracted content. Careers pages often
  list many roles; landing on the page isn't the same as landing on the
  right posting.
- **Extraction:** an LLM call over the confirmed page's text, asked to
  pull out the full job description and, separately, whether the JD
  itself states a salary or range — as an explicit found/not-found signal
  the call returns, not something inferred later from whether a number
  happens to come back non-null.
- **Graceful degrade:** if nothing above works, keep Adzuna's snippet
  (`description_source = 'adzuna_snippet'`) rather than blocking on a page
  that won't cooperate. Let the human glance at the original
  `redirect_url` themselves in the rare case it's worth it.

**Cost controls on the LLM calls.** An early Phase 1 test run burned ~5% of
a $25 API budget on a single posting before these existed -- tier 3 is a
multi-turn agentic loop (web search + fetch tool calls) and, left
unbounded on model choice/turns/spend, gets expensive fast, especially
since a meaningful fraction of postings hit tier 3 in practice (Adzuna's
own `/land/ad/` redirect pages frequently don't resolve to the real
posting on a plain fetch). All three LLM calls (confirm, extract, tier 3's
fallback search) must set:
- **Model: Haiku** (currently `claude-haiku-4-5-20251001`), not whatever
  the CLI defaults to. These are narrow judgment/lookup tasks -- same-page
  confirmation, text extraction, "find this company's careers page" -- not
  tasks that need a frontier model's reasoning.
- **A hard per-call budget cap** (`max_budget_usd`, currently `0.15`) so
  one runaway agentic loop (tier 3 chasing dead-end searches) can't blow
  through real money. The SDK stops the query and returns an
  `error_max_budget_usd` result when hit -- treat that exactly like any
  other failed capture attempt (graceful degrade, not a crash).
- **A capped turn budget on tier 3** (`max_turns`, currently `8`) --
  confirm/extract are single-turn and don't need this, but tier 3's
  search-then-fetch loop does.
- **A wall-clock timeout per call** (currently 120s) so a stalled query
  degrades gracefully instead of hanging the whole discovery run.

Before scaling up `results_per_page` on a real run, smoke-test with a
small page size first and sanity-check actual spend -- these caps bound
the *worst case* per call, not the total cost of a large run.

**URL recorded.** `url` stores whichever page the JD was actually captured
from, not just Adzuna's original link -- this tracks `description_source`:
- `description_source = 'redirect_url'` (tier 1/2 succeeded) or
  `'adzuna_snippet'` (nothing succeeded) -- `url` stays Adzuna's
  `redirect_url`, since that's the page that was actually used (or the best
  link available to hand the human).
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
└── requirements.txt          # include playwright + `playwright install chromium` in setup notes
```

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
   standalone (see the throwaway `explore_adzuna.py` exploration script
   for the actual response shape before wiring it into MCP).
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
