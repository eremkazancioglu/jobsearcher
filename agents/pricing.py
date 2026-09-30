"""Claude Haiku 4.5 pricing and usage -> dollars, shared by the raw-SDK call
sites (fetchers.py, digest_search.py) instead of each keeping its own copy
of the constants. The raw Anthropic SDK, unlike claude_agent_sdk's
ResultMessage, doesn't return a pre-computed total_cost_usd, so cost is
derived locally from each response's `usage` block.

Two things are easy to miss, both confirmed by measuring real calls:
- cache writes are billed at 1.25x input (5-minute TTL cache);
- the web_search tool charges a flat fee per search ($10 per 1,000) ON TOP
  of token costs -- a response's tokens alone understate a search call by
  the fee, about 40% of a typical search's real cost. The count comes back
  in usage.server_tool_use.web_search_requests.
"""

PRICE_INPUT = 1.0 / 1_000_000
PRICE_CACHE_WRITE_5M = 1.25 / 1_000_000
PRICE_CACHE_READ = 0.10 / 1_000_000
PRICE_OUTPUT = 5.0 / 1_000_000
PRICE_WEB_SEARCH = 10.0 / 1_000


def usage_cost_usd(usage) -> float:
    server_tool_use = getattr(usage, "server_tool_use", None)
    searches = getattr(server_tool_use, "web_search_requests", 0) or 0
    return (
        (usage.input_tokens or 0) * PRICE_INPUT
        + (getattr(usage, "cache_creation_input_tokens", None) or 0) * PRICE_CACHE_WRITE_5M
        + (getattr(usage, "cache_read_input_tokens", None) or 0) * PRICE_CACHE_READ
        + (usage.output_tokens or 0) * PRICE_OUTPUT
        + searches * PRICE_WEB_SEARCH
    )
