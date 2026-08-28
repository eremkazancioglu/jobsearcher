"""Postgres access -- psycopg v3, sync.

Phase 1 needs two operations: check whether a posting's already on file
(the dedup-before-capture check) and insert a newly captured one. The
`unique(source, external_id)` constraint in schema.sql is the backstop if
these are ever raced; posting_exists() is what avoids re-running the
expensive capture flow for postings already written.

Phase 2 adds: reading postings still awaiting fit categorization, writing
that judgment back, logging each agent run (agents/common.py's
AgentRunTracker), and the reads/writes dashboard/app.py needs (new
matches, the applied-to pipeline, mark-as-applied, event history, recent
run history for the pipeline health tab).
"""

import os

import psycopg
from psycopg.rows import class_row
from dotenv import load_dotenv

from models.schema import AgentRun, ApplicationEvent, Posting

load_dotenv(override=True)

DATABASE_URL = os.environ["DATABASE_URL"]


def _connect() -> psycopg.Connection:
    """The only place DATABASE_URL is ever passed to psycopg. A failed
    connection is re-raised as a bare RuntimeError with `from None` --
    libpq/psycopg connection-error text can echo back connection
    parameters, and this runs in GitHub Actions on a public repo where
    workflow logs are public; an uncaught traceback is the realistic way a
    secret leaks, not a deliberate print. Every function below goes through
    this instead of calling psycopg.connect(DATABASE_URL) directly."""
    try:
        return psycopg.connect(DATABASE_URL)
    except psycopg.OperationalError:
        raise RuntimeError("Database connection failed (error details withheld from logs)") from None


def posting_exists(source: str, external_id: str) -> bool:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "select 1 from postings where source = %s and external_id = %s",
                (source, external_id),
            )
            return cur.fetchone() is not None


def insert_posting(posting: Posting) -> None:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into postings (
                    source, external_id, title, company, location, url,
                    description, description_source, salary_min, salary_max,
                    salary_is_predicted, work_location
                ) values (
                    %(source)s, %(external_id)s, %(title)s, %(company)s,
                    %(location)s, %(url)s, %(description)s,
                    %(description_source)s, %(salary_min)s, %(salary_max)s,
                    %(salary_is_predicted)s, %(work_location)s
                )
                """,
                posting.model_dump(
                    include={
                        "source",
                        "external_id",
                        "title",
                        "company",
                        "location",
                        "url",
                        "description",
                        "description_source",
                        "salary_min",
                        "salary_max",
                        "salary_is_predicted",
                        "work_location",
                    }
                ),
            )
        conn.commit()


def fetch_uncategorized_postings() -> list[Posting]:
    """Postings still awaiting fit categorization -- categorize.py's input.
    Oldest first, so a run that doesn't get through everything still makes
    forward progress on the longest-waiting postings first."""
    with _connect() as conn:
        with conn.cursor(row_factory=class_row(Posting)) as cur:
            cur.execute("select * from postings where match_category is null order by discovered_at asc")
            return cur.fetchall()


def update_posting_category(posting_id, match_category: str, match_notes: str) -> None:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "update postings set match_category = %s, match_notes = %s where id = %s",
                (match_category, match_notes, posting_id),
            )
        conn.commit()


def insert_agent_run(run: AgentRun) -> None:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into agent_runs (
                    agent_name, started_at, finished_at, status,
                    items_processed, items_new, error_message
                ) values (
                    %(agent_name)s, %(started_at)s, %(finished_at)s, %(status)s,
                    %(items_processed)s, %(items_new)s, %(error_message)s
                )
                """,
                run.model_dump(
                    include={
                        "agent_name",
                        "started_at",
                        "finished_at",
                        "status",
                        "items_processed",
                        "items_new",
                        "error_message",
                    }
                ),
            )
        conn.commit()


def fetch_new_matches(since_hours: int | None = None) -> list[Posting]:
    """strong/mixed postings not yet applied to or dismissed -- the
    dashboard's "new matches" tab. Newest first, so the freshest matches
    surface at top. since_hours (e.g. 24/72/168 for the dashboard's
    24h/3d/7d lookback options) filters to postings discovered within that
    window; None (the default for any caller besides the dashboard) means
    no lookback filter -- everything not yet applied to or dismissed,
    regardless of age."""
    query = (
        "select * from postings where match_category in ('strong','mixed') "
        "and applied_at is null and dismissed_at is null"
    )
    params = ()
    if since_hours is not None:
        query += " and discovered_at >= now() - (%s * interval '1 hour')"
        params = (since_hours,)
    query += " order by discovered_at desc"
    with _connect() as conn:
        with conn.cursor(row_factory=class_row(Posting)) as cur:
            cur.execute(query, params)
            return cur.fetchall()


def dismiss_posting(posting_id) -> None:
    """Human "not interested" signal from the dashboard -- distinct from
    mark_as_applied. Doesn't delete the posting, just drops it out of "new
    matches"; a notes/reason field can be added later without changing this
    shape (not built yet -- not needed until it's actually wanted)."""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("update postings set dismissed_at = now() where id = %s", (posting_id,))
        conn.commit()


def fetch_undigested_matches() -> list[Posting]:
    """strong/mixed postings not yet sent in a Slack digest -- send_digest.py's
    input. Filtered to applied_at/dismissed_at is null too: no point
    digesting something the human has already acted on by the time the
    digest agent gets to it (e.g. dismissed straight from the dashboard
    before a digest ran)."""
    with _connect() as conn:
        with conn.cursor(row_factory=class_row(Posting)) as cur:
            cur.execute(
                "select * from postings where match_category in ('strong','mixed') "
                "and applied_at is null and dismissed_at is null and digested_at is null "
                "order by discovered_at asc"
            )
            return cur.fetchall()


def mark_digested(posting_ids: list) -> None:
    """Bookkeeping only (see schema.sql's digested_at comment) -- called
    once after a digest send succeeds, so a posting is never included in
    more than one digest."""
    if not posting_ids:
        return
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "update postings set digested_at = now() where id = any(%s)",
                (posting_ids,),
            )
        conn.commit()


def fetch_pipeline() -> list[Posting]:
    """Postings already applied to -- the dashboard's "pipeline" tab."""
    with _connect() as conn:
        with conn.cursor(row_factory=class_row(Posting)) as cur:
            cur.execute("select * from postings where applied_at is not null order by applied_at desc")
            return cur.fetchall()


def mark_as_applied(posting_id) -> None:
    """The only way applied_at/application_status get set -- a direct human
    action from the dashboard, never inferred by an agent (see CLAUDE.md's
    "Why applied_at is set by the human, not inferred by an agent")."""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "update postings set applied_at = now(), application_status = 'applied' where id = %s",
                (posting_id,),
            )
        conn.commit()


def remove_from_pipeline(posting_id) -> None:
    """The inverse of mark_as_applied -- clears applied_at/application_status
    back to their pre-applied defaults and drops any application_events
    logged against this posting (those only make sense for an application
    that's actually being tracked). The posting itself is never deleted --
    it reappears in "new matches" if still strong/mixed, same as any other
    uncategorized-into-applied posting would. Same "human decides, agent
    never infers" reasoning as mark_as_applied -- this is a correction a
    person makes, not something derived."""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("delete from application_events where posting_id = %s", (posting_id,))
            cur.execute(
                "update postings set applied_at = null, application_status = 'not_applied' where id = %s",
                (posting_id,),
            )
        conn.commit()


def fetch_application_events(posting_id) -> list[ApplicationEvent]:
    with _connect() as conn:
        with conn.cursor(row_factory=class_row(ApplicationEvent)) as cur:
            cur.execute(
                "select * from application_events where posting_id = %s order by detected_at desc",
                (posting_id,),
            )
            return cur.fetchall()


def fetch_recent_agent_runs(limit: int = 50) -> list[AgentRun]:
    """Most recent agent_runs rows -- the dashboard's "pipeline health" tab."""
    with _connect() as conn:
        with conn.cursor(row_factory=class_row(AgentRun)) as cur:
            cur.execute("select * from agent_runs order by started_at desc limit %s", (limit,))
            return cur.fetchall()
