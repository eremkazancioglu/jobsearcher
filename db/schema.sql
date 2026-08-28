-- Drops and recreates every table -- running this wipes all existing data
-- (postings, agent_runs, application_events). Meant for a clean/reset run
-- against a dev database, not a way to apply incremental changes to one
-- already holding real captured postings.
drop table if exists application_events cascade;
drop table if exists agent_runs cascade;
drop table if exists postings cascade;

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
    dismissed_at timestamptz,              -- human dismissal from "new matches" (Streamlit) --
                                            -- distinct from applied_at: a dismiss says "not
                                            -- interested", not "already applied"
    digested_at timestamptz,               -- set by send_digest.py once included in a Slack
                                            -- digest -- agent-set bookkeeping, not a human
                                            -- signal like dismissed_at/applied_at; keeps a
                                            -- posting from being sent more than once
    discovered_at timestamptz not null default now(),
    applied_at timestamptz,                -- set by the human, via Streamlit
    application_status text not null default 'not_applied'
        check (application_status in
            ('not_applied','applied','interviewing','rejected','offer','withdrawn')),
    unique (source, external_id)
);

create table agent_runs (
    id bigserial primary key,
    agent_name text not null,
    started_at timestamptz not null,
    finished_at timestamptz not null,
    status text not null check (status in ('success','partial','failed')),
    items_processed integer not null default 0,
    items_new integer not null default 0,
    llm_errors integer not null default 0,  -- Claude API calls that raised outright
                                             -- (rate limit, out of credits, budget cap,
                                             -- auth) -- distinct from a posting merely
                                             -- degrading gracefully to a lower tier
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
