"""Loads the user's job preferences text from PREFERENCES_PATH.

Preferences answer "what do I want" (role/seniority, location/remote
stance, comp floor, industries to avoid, deal-breakers) as opposed to the
resume, which answers "what am I qualified for" -- see CLAUDE.md's "Fit
categorization" for why these are kept as two separate documents. Same
gitignored personal/ folder and .env-var pattern as agents/resume.py.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)

PREFERENCES_PATH = os.environ["PREFERENCES_PATH"]


def load_preferences() -> str:
    path = Path(PREFERENCES_PATH)
    if not path.exists():
        raise FileNotFoundError(
            f"Preferences not found at {path} (set PREFERENCES_PATH in .env to point at it)"
        )
    return path.read_text(encoding="utf-8")
