"""Shared agent-run tracking: agent_runs + STATUS.json, in one place so
every Phase 2 agent logs consistently instead of hand-rolling this.

    async with AgentRunTracker("categorize") as run:
        for posting in postings:
            ...
            run.record(is_new=True)
        # or, on a per-item failure that shouldn't fail the whole run:
        run.record_error("posting abc123: Claude call failed")

On exit, writes one row to `agent_runs` and updates this agent's entry in
STATUS.json (a flat file at the repo root -- committed by the GitHub
Actions workflow as its last step in Phase 2's scheduling setup, not by
this code). Status is "failed" if the run raised, "partial" if it
completed but recorded at least one item-level error, "success" otherwise.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from db.db import insert_agent_run
from models.schema import AgentRun

logger = logging.getLogger(__name__)

STATUS_PATH = Path(__file__).resolve().parent.parent / "STATUS.json"


class AgentRunTracker:
    def __init__(self, agent_name: str):
        self.agent_name = agent_name
        self.items_processed = 0
        self.items_new = 0
        self._errors: list[str] = []
        self._started_at: Optional[datetime] = None

    def record(self, is_new: bool = False) -> None:
        self.items_processed += 1
        if is_new:
            self.items_new += 1

    def record_error(self, message: str) -> None:
        self._errors.append(message)
        logger.warning("%s: %s", self.agent_name, message)

    async def __aenter__(self) -> "AgentRunTracker":
        self._started_at = datetime.now(timezone.utc)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        finished_at = datetime.now(timezone.utc)
        if exc is not None:
            status = "failed"
            error_message = str(exc)
        elif self._errors:
            status = "partial"
            error_message = "; ".join(self._errors)
        else:
            status = "success"
            error_message = None

        run = AgentRun(
            agent_name=self.agent_name,
            started_at=self._started_at,
            finished_at=finished_at,
            status=status,
            items_processed=self.items_processed,
            items_new=self.items_new,
            error_message=error_message,
        )
        insert_agent_run(run)
        _update_status_json(run)
        logger.info(
            "%s run %s: %d processed, %d new%s",
            self.agent_name, status, self.items_processed, self.items_new,
            f" ({len(self._errors)} error(s))" if self._errors else "",
        )
        # Don't suppress the exception -- __aexit__ returning None/False
        # lets it propagate as normal.


def _update_status_json(run: AgentRun) -> None:
    """Merge this run's result into STATUS.json's per-agent entries, rather
    than overwriting the whole file -- other agents' last-run entries stay
    intact when only one agent runs."""
    status = {}
    if STATUS_PATH.exists():
        try:
            status = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("STATUS.json was malformed -- overwriting")
    status[run.agent_name] = {
        "status": run.status,
        "started_at": run.started_at.isoformat(),
        "finished_at": run.finished_at.isoformat(),
        "items_processed": run.items_processed,
        "items_new": run.items_new,
        "error_message": run.error_message,
    }
    STATUS_PATH.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
