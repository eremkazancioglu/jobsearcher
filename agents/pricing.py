"""Claude Haiku pricing and usage -> dollars, shared by the raw-SDK call
sites (fetchers.py) instead of each keeping its own copy of the constants.
The raw Anthropic SDK, unlike claude_agent_sdk's ResultMessage, doesn't
return a pre-computed total_cost_usd, so cost is derived locally from each
response's `usage` block.

Prices are per model, so switching a call site's model means changing its
model ID and nothing here -- as long as the model has an entry. An unknown
model raises KeyError on purpose: silently pricing a new model at an old
model's rates would make every logged cost wrong.

Things that are easy to miss, all confirmed against the published pricing
page and real calls:
- cache writes are billed at 1.25x input (5-minute cache);
- the web_search tool charges a flat fee per search ($10 per 1,000) ON TOP
  of token costs, read from usage.server_tool_use.web_search_requests
  (nothing here uses web_search any more -- search is Brave now -- but the
  field is still handled in case a call ever does);
- Haiku 5.5 has a second, pricier tier for prompts over 100,000 tokens.
  Nothing in this project comes close (inputs are capped well under 20k
  tokens), but the tier is implemented so a bug that blows up an input
  size shows up in the logged cost instead of hiding behind the cheap rate;
- Haiku 5.5 (like every model since 4.7) uses a tokenizer that produces
  about 30% more tokens for the same text than Haiku 4.5, so per-token
  prices understate the comparison: roughly 7x cheaper in practice, not 10x.
"""

from dataclasses import dataclass

PRICE_WEB_SEARCH = 10.0 / 1_000
LONG_PROMPT_THRESHOLD_TOKENS = 100_000


@dataclass(frozen=True)
class Prices:
    """USD per token."""

    input: float
    cache_write_5m: float
    cache_read: float
    output: float


HAIKU_4_5 = Prices(1.0e-6, 1.25e-6, 0.10e-6, 5.0e-6)
HAIKU_5_5 = Prices(0.10e-6, 0.125e-6, 0.01e-6, 0.50e-6)
HAIKU_5_5_LONG = Prices(0.50e-6, 0.625e-6, 0.05e-6, 2.50e-6)

# model ID -> (standard prices, prices for prompts over 100k tokens or None
# when the model has no separate long-prompt tier)
MODEL_PRICES: dict[str, tuple[Prices, Prices | None]] = {
    "claude-haiku-4-5-20251001": (HAIKU_4_5, None),
    "claude-haiku-5-5": (HAIKU_5_5, HAIKU_5_5_LONG),
}


def usage_cost_usd(usage, model: str) -> float:
    standard, long_tier = MODEL_PRICES[model]
    cache_write = getattr(usage, "cache_creation_input_tokens", None) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", None) or 0
    input_tokens = usage.input_tokens or 0
    prices = standard
    if long_tier and input_tokens + cache_write + cache_read > LONG_PROMPT_THRESHOLD_TOKENS:
        prices = long_tier
    server_tool_use = getattr(usage, "server_tool_use", None)
    searches = getattr(server_tool_use, "web_search_requests", 0) or 0
    return (
        input_tokens * prices.input
        + cache_write * prices.cache_write_5m
        + cache_read * prices.cache_read
        + (usage.output_tokens or 0) * prices.output
        + searches * PRICE_WEB_SEARCH
    )
