"""Single entrypoint chaining the three Phase 2 stages in order:
discovery -> categorize -> digest.

    uv run scripts/run_pipeline.py

Discovery search params pass through, same names/meaning/defaults as
agents/discovery.py's own CLI (see there for the actual query these
defaults reproduce) -- a bare invocation matches a bare
`uv run agents/discovery.py`:

    uv run scripts/run_pipeline.py --what "data scientist" --where "united states"

Each stage runs as its own subprocess, not an in-process import -- this
script doesn't parse discovery.py's/categorize.py's/send_digest.py's
internals or reuse their argparse, it just runs each CLI in order and
checks the exit code. A stage failing doesn't abort the run: later stages
don't depend on *this run's* discovery output specifically (categorize
picks up whatever's uncategorized regardless of which run wrote it, and
digest picks up whatever's undigested) -- so every stage is attempted
regardless of an earlier one's outcome, but the script exits non-zero if
any stage failed, so CI/cron surfaces it instead of reporting false
success.
"""

import argparse
import asyncio
import logging
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.common import AgentRunTracker

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run discovery -> categorize -> digest in sequence")
    parser.add_argument("--what", default="remote", help="Keyword(s) to search for")
    parser.add_argument("--where", default="united states", help="Location to search in")
    parser.add_argument("--salary-min", default="200000")
    parser.add_argument("--max-days-old", default="3")
    parser.add_argument("--results-per-page", default="20")
    parser.add_argument("--max-pages", default="5")
    parser.add_argument("--title-only", default="data scientist")
    parser.add_argument("--full-time", default="1", choices=["0", "1"])
    return parser.parse_args()


def _discovery_cmd(args: argparse.Namespace) -> list[str]:
    cmd = [
        "uv", "run", "agents/discovery.py",
        "--what", args.what,
        "--where", args.where,
        "--max-days-old", str(args.max_days_old),
        "--results-per-page", str(args.results_per_page),
        "--max-pages", str(args.max_pages),
        "--full-time", str(args.full_time),
    ]
    if args.salary_min is not None:
        cmd += ["--salary-min", str(args.salary_min)]
    if args.title_only is not None:
        cmd += ["--title-only", args.title_only]
    return cmd


def _run_stage(name: str, cmd: list[str]) -> bool:
    logger.info("=== Starting stage: %s ===", name)
    result = subprocess.run(cmd, cwd=REPO_ROOT)
    ok = result.returncode == 0
    logger.info("=== Stage %s %s ===", name, "succeeded" if ok else f"FAILED (exit {result.returncode})")
    return ok


async def main() -> None:
    args = parse_args()
    stages = [
        ("discovery", _discovery_cmd(args)),
        ("categorize", ["uv", "run", "agents/categorize.py"]),
        ("digest", ["uv", "run", "digest/send_digest.py"]),
    ]

    async with AgentRunTracker("pipeline") as run:
        failed = []
        for name, cmd in stages:
            ok = _run_stage(name, cmd)
            run.record()
            if not ok:
                failed.append(name)
                run.record_error(f"{name} stage exited non-zero")

    if failed:
        logger.error("Pipeline run finished with failed stage(s): %s", ", ".join(failed))
        sys.exit(1)
    logger.info("Pipeline run complete -- all stages succeeded.")


if __name__ == "__main__":
    asyncio.run(main())
