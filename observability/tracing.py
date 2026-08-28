"""Langfuse client + environment config -- imported once by every module
that makes an LLM call, so tracing is wired up in one place rather than
re-derived per agent.

Two distinct LLM call shapes in this project need two distinct
instrumentation approaches:

- `categorize.py` calls the raw Anthropic Python SDK directly
  (`client.messages.create()`) -- `AnthropicInstrumentor` auto-captures
  model/input/output/usage for every such call, no per-call-site code
  needed beyond importing this module once.
- `fetchers.py` calls go through `claude_agent_sdk`, which launches a CLI
  subprocess rather than calling a Python Anthropic client object --
  OTel auto-instrumentation can't see inside that. Those call sites log a
  manual generation instead (see `_run_claude_json()` in fetchers.py),
  using `ResultMessage.usage`/`total_cost_usd`, which the SDK already
  computes -- not re-derived or estimated here.

Importing this module is enough to get both working; nothing else needs
to change at each LLM call site beyond fetchers.py's one shared helper.
"""

import logging
import os

from dotenv import load_dotenv
from langfuse import get_client
from opentelemetry.instrumentation.anthropic import AnthropicInstrumentor

load_dotenv(override=True)

# development locally, production in CI -- keeps local test runs visually
# separate from real scheduled ones in the Langfuse UI, without needing a
# second Langfuse project. Read directly by the SDK itself (env var name
# is Langfuse's own); this only supplies a default when it's unset, e.g.
# a bare local `uv run agents/categorize.py`.
os.environ.setdefault("LANGFUSE_TRACING_ENVIRONMENT", "development")

# Tracing is optional (LANGFUSE_PUBLIC_KEY/SECRET_KEY unset is a supported,
# quiet state -- same "falls back gracefully" treatment as SLACK_WEBHOOK_URL
# being unset for the digest) -- get_client() still returns a usable
# (auto-disabled, no-op) client either way, but logs an "Authentication
# error" warning on every call otherwise. Silence that one known, expected
# case specifically, not Langfuse's logger generally.
if not os.environ.get("LANGFUSE_PUBLIC_KEY"):
    logging.getLogger("langfuse").setLevel(logging.ERROR)

langfuse = get_client()

AnthropicInstrumentor().instrument()
