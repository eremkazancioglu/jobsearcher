# Methodology

This document describes, in plain terms, how the job-hunt pipeline
actually works. It's meant to be readable without knowing the codebase --
for the full technical spec and the reasoning behind each decision, see
`CLAUDE.md`.

The pipeline is being built in phases. This document currently covers
**Phase 1: discovery and full posting capture** -- finding job postings
and recovering their complete descriptions. It will grow as later phases
(fit assessment, company research, application tracking) are built.

## Overview

Phase 1 has two jobs:

1. **Discovery** -- find job postings that match a search (keyword,
   location, and filters like salary or remote/full-time).
2. **Capture** -- for each posting found, recover the *full* job
   description, not just the short teaser a job board shows.

Discovery is the easy part -- one API call. Capture is the hard part: job
boards only ever show a snippet, so getting the real posting means
tracking it down and reading it, and every site is laid out differently.

## Step 1: Discovery

The pipeline searches Adzuna, a job-listing aggregator, for postings
matching a keyword and location. A handful of filters narrow this down
further: how recent a posting must be, a minimum salary, whether to
restrict the keyword match to the job title specifically, and whether to
require full-time roles.

Two things keep this from turning into an unbounded amount of work:

- **A hard cap on how many postings get pulled in per run.** Results come
  back a page at a time, and a run only ever fetches up to a fixed number
  of pages, however many postings actually match the search. If a search
  turns out to match more than that cap covers, the run doesn't quietly
  stop partway through and pretend it got everything -- it says plainly
  how much it covered and how much it didn't. The true total match count
  is checked and logged for visibility either way, but it's the page cap,
  not the count, that actually bounds the work done.
- **Has this posting already been captured?** Every posting search turns
  up is checked against what's already been saved. Anything already on
  file is skipped -- there's no reason to redo the expensive work of
  capturing a posting a second time.

Separately, there's a manual sanity check available before committing to
a real run: asking just for the match count, with no fetching at all, and
seeing upfront whether that count would fit within the page cap. This is
opt-in, not something a run does for itself automatically.

Only genuinely new postings move on to capture.

## Step 2: Capture

This is where most of the actual work happens. For each new posting, the
goal is the same: get the complete job description, the real salary if
one is stated, and whether the role is remote, hybrid, or onsite. The
approach is tiered -- try the cheap, reliable path first, and only fall
back to something more expensive when it doesn't work.

### Tier 1 -- go straight to the source

Every posting comes with a link back to Adzuna's own page for it. That
page turns out to already contain a clean, structured copy of the full
job description -- job sites publish this kind of structured data for
their own purposes (so job postings show up properly in Google search
results), and it happens to be exactly what's needed here. When it's
present, it's used directly: no guesswork, no cost, and it doubles as
confirmation that the page is showing a real, current posting rather than
an error page or an expired listing.

Salary, in this case, is simply taken from Adzuna's own data rather than
re-extracted -- Adzuna already computed it, so there's nothing to
re-derive on this path.

Whether the role is remote is checked the same way: Adzuna visibly tags
remote postings on its own page, so that tag is read directly when it's
present. There's no equivalent tag for hybrid or onsite roles, so when no
remote tag is found, a small, narrowly-scoped step reads the job
description and classifies it as remote, hybrid, onsite, or unstated --
deliberately kept minimal so it stays cheap even though it's a real
judgment call.

If the page doesn't yield a valid, structured description at all -- it's
blocked, broken, or the listing has since been taken down -- the pipeline
doesn't guess. It moves on to the next tier.

### Tier 2 -- render the page properly

Some pages need a real browser to show their content at all (the
description only appears after some on-page JavaScript runs, which a
plain fetch doesn't execute). When a page's content comes back
suspiciously thin, it's tried again through a headless browser that
actually renders the page the way a person's browser would, then the same
structured-data check from Tier 1 is applied to what that produces.

### Tier 3 -- search for the posting elsewhere

If Adzuna's own page still doesn't produce a usable result, the last
resort is to go find the posting independently: a plain web search for
the job title and company name, the same kind of search a person would
type in by hand.

That search returns a handful of candidate pages, and each is checked in
order, most-likely match first. For each candidate, the same tiered fetch
from Tier 1/2 is used to get its content, and then -- because this time
there's real uncertainty about whether a given page is actually the right
posting, as opposed to a similar-looking one or a company's general
careers page -- an AI judgment call reviews it: does this page actually
show live content, and is it genuinely the same posting being searched
for? Only if both are true does it extract the description, check for a
stated salary, and read the remote/hybrid/onsite arrangement from the
text.

The first candidate that passes wins. If none do, the search stops there.

### Falling back gracefully

If every tier comes up empty -- the posting can't be confirmed anywhere --
the pipeline doesn't block or fail. It keeps whatever short snippet Adzuna
originally provided, records that this posting fell back to the snippet,
and moves on. A posting with an incomplete description is a normal,
expected outcome for some fraction of postings, not something to treat as
an error.

## What gets saved

For every posting, whether captured in full or not, the pipeline records:
the title, company, and location; the best description it could recover
and where that description actually came from (Adzuna's own page, an
independently-found company page, or just the original snippet); salary
information, along with whether that number was actually stated on the
page or is an estimate; and the remote/hybrid/onsite classification, when
one could be determined.
