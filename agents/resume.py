"""Loads the user's resume text from RESUME_PATH.

The resume itself lives outside version control (personal/ is gitignored --
personal data) -- this just points at wherever RESUME_PATH says it is,
same pattern as every other required credential/config in this project.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)

RESUME_PATH = os.environ["RESUME_PATH"]


def load_resume() -> str:
    path = Path(RESUME_PATH)
    if not path.exists():
        raise FileNotFoundError(
            f"Resume not found at {path} (set RESUME_PATH in .env to point at it)"
        )
    return path.read_text(encoding="utf-8")
