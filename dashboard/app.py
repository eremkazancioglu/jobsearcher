"""Streamlit dashboard: new matches / pipeline / pipeline health.

    uv run streamlit run dashboard/app.py

Three tabs, per CLAUDE.md's Phase 2 spec:
    - New matches: strong/mixed postings not yet applied to or dismissed,
      with a discovered-within lookback filter (24h/3d/7d) and a
      mark-as-applied action. This is the only place applied_at gets set --
      always a direct human action, never inferred by an agent (see
      CLAUDE.md's "Why applied_at is set by the human, not inferred by an
      agent"). Also a dismiss action ("not interested") -- doesn't delete
      the posting, just drops dismissed_at so it stops showing up here; no
      reason/notes field yet (not needed until it's actually wanted).
    - Pipeline: postings already applied to, current status, event history,
      and a remove-from-pipeline action (the inverse of mark-as-applied --
      clears applied_at/application_status and drops event history, does
      not delete the posting; a two-step confirm since it's destructive to
      event history).
    - Pipeline health: recent agent_runs, so a failed/partial run is
      visible without reading logs.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st

from db.db import (
    dismiss_posting,
    fetch_application_events,
    fetch_new_matches,
    fetch_pipeline,
    fetch_recent_agent_runs,
    mark_as_applied,
    remove_from_pipeline,
)

st.set_page_config(page_title="JobSearcher", layout="wide")
st.title("JobSearcher")
# Adzuna's terms require crediting "The Adzuna API" with a link back
# wherever the data is published -- see CLAUDE.md's "Why Adzuna" section.
st.caption("Job data sourced via [the Adzuna API](https://www.adzuna.com).")

tab_new, tab_pipeline, tab_health = st.tabs(["New matches", "Pipeline", "Pipeline health"])

MATCH_BADGE = {"strong": "🟢 strong", "mixed": "🟡 mixed"}
LOOKBACK_OPTIONS = {"Last 24 hours": 24, "Last 3 days": 72, "Last 7 days": 168}


def _format_salary(posting) -> str | None:
    if not posting.salary_min:
        return None
    salary = f"${posting.salary_min:,.0f}-${posting.salary_max:,.0f}"
    if posting.salary_is_predicted:
        salary += " (estimated)"
    return salary


with tab_new:
    lookback_label = st.radio("Discovered within", list(LOOKBACK_OPTIONS), index=2, horizontal=True)
    postings = fetch_new_matches(since_hours=LOOKBACK_OPTIONS[lookback_label])
    if not postings:
        st.info("No new strong/mixed matches waiting on a decision in this window.")
    for posting in postings:
        with st.container(border=True):
            col_main, col_action = st.columns([5, 1])
            with col_main:
                st.markdown(
                    f"**[{posting.title}]({posting.url})** at **{posting.company}** "
                    f"-- {MATCH_BADGE.get(posting.match_category, posting.match_category)}"
                )
                meta = [posting.location or "location unknown"]
                if posting.work_location:
                    meta.append(posting.work_location)
                salary = _format_salary(posting)
                if salary:
                    meta.append(salary)
                st.caption(" · ".join(meta))
                if posting.match_notes:
                    st.write(posting.match_notes)
            with col_action:
                if st.button("Mark applied", key=f"apply-{posting.id}"):
                    mark_as_applied(posting.id)
                    st.rerun()
                if st.button("Dismiss", key=f"dismiss-{posting.id}"):
                    dismiss_posting(posting.id)
                    st.rerun()

with tab_pipeline:
    postings = fetch_pipeline()
    if not postings:
        st.info("No applications tracked yet -- mark a posting as applied from the New matches tab.")
    for posting in postings:
        with st.container(border=True):
            col_main, col_action = st.columns([5, 1])
            with col_main:
                st.markdown(f"**[{posting.title}]({posting.url})** at **{posting.company}**")
                applied_str = posting.applied_at.strftime("%Y-%m-%d") if posting.applied_at else "unknown"
                st.caption(f"Status: {posting.application_status} · Applied {applied_str}")
                events = fetch_application_events(posting.id)
                if events:
                    with st.expander(f"{len(events)} event(s)"):
                        for event in events:
                            line = f"{event.detected_at:%Y-%m-%d} -- {event.event_type}"
                            if event.notes:
                                line += f": {event.notes}"
                            st.write(line)
            with col_action:
                # Two-step confirm -- this also drops event history
                # (remove_from_pipeline), so a single misclick shouldn't be
                # able to do that.
                confirm_key = f"confirm-remove-{posting.id}"
                if st.session_state.get(confirm_key):
                    st.caption("Drops event history too.")
                    if st.button("Confirm remove", key=f"confirm-{posting.id}"):
                        remove_from_pipeline(posting.id)
                        st.session_state.pop(confirm_key, None)
                        st.rerun()
                    if st.button("Cancel", key=f"cancel-{posting.id}"):
                        st.session_state.pop(confirm_key, None)
                        st.rerun()
                else:
                    if st.button("Remove from pipeline", key=f"remove-{posting.id}"):
                        st.session_state[confirm_key] = True
                        st.rerun()

with tab_health:
    runs = fetch_recent_agent_runs()
    if not runs:
        st.info("No agent runs logged yet.")
    else:
        st.dataframe(
            [
                {
                    "agent": r.agent_name,
                    "status": r.status,
                    "started_at": r.started_at,
                    "finished_at": r.finished_at,
                    "processed": r.items_processed,
                    "new": r.items_new,
                    "error": r.error_message,
                }
                for r in runs
            ],
            use_container_width=True,
        )
