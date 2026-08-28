# JobSearcher

A multi-agent pipeline that discovers job postings, recovers their full
descriptions, assesses fit against a resume *and* a preferences document,
and surfaces the good matches via a dashboard and a Slack digest.

Job data is sourced via [the Adzuna API](https://www.adzuna.com).

For the full design (data model, every decision's reasoning, what's built
vs. planned) see [`CLAUDE.md`](CLAUDE.md). For a plain-language walkthrough
of how it actually works, see [`METHODOLOGY.md`](METHODOLOGY.md).

## What's here

- **Discovery + capture** (`agents/discovery.py`, `agents/fetchers.py`) --
  search Adzuna, then recover each posting's full description (tiered:
  direct fetch, headless render, fallback web search).
- **Fit categorization** (`agents/categorize.py`) -- judges strong / mixed
  / weak fit against both a resume and a preferences document.
- **Dashboard** (`dashboard/app.py`, Streamlit) -- new matches (with a
  lookback filter, mark-applied/dismiss actions), an applied-to pipeline,
  and a run-health view.
- **Slack digest** (`digest/send_digest.py`) -- one message per run
  covering everything newly matched; falls back to stdout if no Slack
  webhook is configured.
- **`scripts/run_pipeline.py`** -- chains discovery -> categorize ->
  digest into a single entrypoint, used both locally and in CI.

Not yet built: company research (deliberately out of scope), the
application tracker (a later, separate phase), Langfuse tracing.

## Setup

Requires [`uv`](https://docs.astral.sh/uv/) and Python 3.12+.

1. **Install dependencies:**

   ```
   uv sync
   uv run playwright install chromium
   ```

   The Playwright install is a separate, one-time step -- `uv sync` only
   installs the Python package, not the browser binary the capture
   fallback tier needs.

2. **Set up a Postgres database.** This project uses
   [Neon](https://neon.tech) (see `CLAUDE.md`'s Environments section for
   why a hosted DB, not local Postgres). Create a project, then apply the
   schema:

   ```
   psql "$DATABASE_URL" -f db/schema.sql
   ```

   `db/schema.sql` drops and recreates every table -- fine for a fresh
   database, **destructive** on one already holding real data (in that
   case, apply schema changes incrementally instead -- see git history for
   examples of non-destructive `alter table` migrations run against a live
   database).

3. **Copy `.env.example` to `.env`** and fill in real values:

   ```
   cp .env.example .env
   ```

   - `ADZUNA_APP_ID` / `ADZUNA_APP_KEY` -- from
     [developer.adzuna.com](https://developer.adzuna.com/).
   - `ANTHROPIC_API_KEY` -- from the
     [Anthropic Console](https://console.anthropic.com/).
   - `DATABASE_URL` -- your Neon connection string.
   - `RESUME_PATH` / `PREFERENCES_PATH` -- point these at real files under
     `personal/` (gitignored -- this is where your actual resume and a
     free-prose preferences doc live; see `agents/preferences.py`'s
     docstring for what a preferences doc should cover).
   - `SLACK_WEBHOOK_URL` -- optional; the digest prints to stdout if unset.

## Running it

Each stage can run standalone:

```
uv run agents/discovery.py                 # search + capture (see --help for query params)
uv run agents/discovery.py --count-only     # just check how many postings match, no capture
uv run agents/categorize.py                 # fit judgment against uncategorized postings
uv run digest/send_digest.py                # send/print the digest of new matches
uv run streamlit run dashboard/app.py       # the dashboard, at localhost:8501
```

Or run the whole discovery -> categorize -> digest sequence in one go:

```
uv run scripts/run_pipeline.py
```

Search parameters (`--what`, `--where`, `--title-only`, `--salary-min`,
`--max-days-old`, etc.) are shared across `discovery.py` and
`run_pipeline.py` -- see either's `--help`.

## Scheduling (GitHub Actions)

`.github/workflows/agents.yml` runs the pipeline in CI, triggerable
manually (`workflow_dispatch`) or on a schedule (currently commented out
pending validation). It needs these repository secrets:

- `ADZUNA_APP_ID`, `ADZUNA_APP_KEY`, `ANTHROPIC_API_KEY`, `DATABASE_URL`,
  `SLACK_WEBHOOK_URL` -- same values as `.env`.
- `RESUME_B64`, `PREFERENCES_B64` -- base64 of your resume/preferences
  files (`personal/` is gitignored, so CI can't check it out directly):

  ```
  base64 -i personal/your_resume.md | pbcopy
  ```

  See `CLAUDE.md`'s "Environment variables / secrets" section for the full
  reasoning.

## Attribution

Job data is sourced via [the Adzuna API](https://www.adzuna.com), per
their terms of service.
