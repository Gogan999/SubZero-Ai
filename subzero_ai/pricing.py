"""What AI usage costs, so the app can show an estimate next to token counts.

To update a price, edit PRICES below: US dollars per million tokens, from https://www.anthropic.com/pricing (or the
provider's own price page). A model that isn't listed falls back to the price the Pi gateway reports for it, and
shows no cost when neither knows.

Assumptions, so the numbers are estimates, not a bill:
- Standard API rates (no Batch discount, no fast mode, no Priority Tier).
- Cache reads use the cache_read price. Writing to the cache costs CACHE_WRITE_FACTOR times the input price (the
  5-minute cache, which is what the app asks for).
- Thinking tokens are already inside output_tokens, which is how the API reports them.
- Each web search costs WEB_SEARCH_PRICE on top of tokens.
- Haiku 5.5's long-prompt rate applies to one reply whose prompt is over 100K tokens. Code sessions add up many
  requests into one total, so they always use the normal rate.
- Models answered through the Claude subscription bridge cost nothing per token (the subscription is a flat fee).
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import normalize_model_id


@dataclass(frozen=True)
class Price:
    input: float  # dollars per million input tokens
    output: float  # dollars per million output tokens
    cache_read: float  # dollars per million tokens read from the prompt cache
    # Some models charge more once one request's prompt passes a size; None means one rate for every size
    long_prompt_over: int | None = None
    long_input: float = 0.0
    long_output: float = 0.0


# Last checked 2026-11-09. Haiku 5.5's cache read price isn't published separately; 10% of input is assumed.
PRICES: dict[str, Price] = {
    "anthropic/claude-opus-5-5": Price(input=4.00, output=20.00, cache_read=0.20),
    "anthropic/claude-sonnet-5-5": Price(input=2.00, output=10.00, cache_read=0.20),
    "anthropic/claude-haiku-5-5": Price(input=0.10, output=0.50, cache_read=0.01,
                                        long_prompt_over=100_000, long_input=0.50, long_output=2.50),
    "anthropic/claude-opus-5": Price(input=5.00, output=25.00, cache_read=0.50),
    "anthropic/claude-sonnet-5": Price(input=2.00, output=10.00, cache_read=0.20),
    "anthropic/claude-opus-4-8": Price(input=5.00, output=25.00, cache_read=0.50),
    "anthropic/claude-sonnet-4-6": Price(input=3.00, output=15.00, cache_read=0.30),
    "anthropic/claude-haiku-4-5": Price(input=1.00, output=5.00, cache_read=0.10),
}
WEB_SEARCH_PRICE = 0.01  # dollars per search ($10 per 1,000)
CACHE_WRITE_FACTOR = 1.25  # writing to the 5-minute prompt cache costs 1.25x the input price
FREE_PROVIDERS = ("claude-bridge",)  # flat-fee subscription: no per-token cost


def price_for(model: str | None, model_info: dict[str, dict] | None = None) -> Price | None:
    """The price of a model: the table above, else what the Pi gateway reports (None when nobody knows)."""
    model_id = normalize_model_id(model)
    if not model_id:
        return None
    if model_id.split("/", 1)[0] in FREE_PROVIDERS:
        return Price(0.0, 0.0, 0.0)
    if model_id in PRICES:
        return PRICES[model_id]
    cost = ((model_info or {}).get(model_id) or {}).get("cost") or {}
    try:
        cost_in, cost_out = float(cost.get("input") or 0), float(cost.get("output") or 0)
        cache = float(cost.get("cacheRead") or 0)
    except (TypeError, ValueError, AttributeError):
        return None
    if not (cost_in or cost_out):
        return None
    return Price(cost_in, cost_out, cache or cost_in)


def cost_usd(model: str | None, *, input_tokens: int | None = 0, output_tokens: int | None = 0,
             cache_read_tokens: int | None = 0, cache_write_tokens: int | None = 0, web_searches: int | None = 0,
             one_request: bool = True, model_info: dict[str, dict] | None = None) -> float | None:
    """Estimated dollars for some usage, or None when the model's price isn't known.
    one_request=False (a total over many requests) never applies a long-prompt rate."""
    price = price_for(model, model_info)
    if price is None:
        return None
    tokens_in, tokens_out = int(input_tokens or 0), int(output_tokens or 0)
    cached, written = int(cache_read_tokens or 0), int(cache_write_tokens or 0)
    rate_in, rate_out = price.input, price.output
    if one_request and price.long_prompt_over is not None and tokens_in + cached + written > price.long_prompt_over:
        rate_in, rate_out = price.long_input, price.long_output
    total = (tokens_in * rate_in + written * rate_in * CACHE_WRITE_FACTOR + tokens_out * rate_out
             + cached * price.cache_read) / 1_000_000
    return total + int(web_searches or 0) * WEB_SEARCH_PRICE


def cost_label(dollars: float | None) -> str:
    """'about $0.42', 'about $0.004' (most single replies cost less than a cent), 'under $0.001', 'no API cost',
    or '' when the cost isn't known."""
    if dollars is None:
        return ""
    if dollars == 0:
        return "no API cost"
    if dollars < 0.001:
        return "under $0.001"
    if dollars < 0.01:
        return f"about ${dollars:.3f}"
    return f"about ${dollars:,.2f}"


def usage_line(tokens: int, dollars: float | None) -> str:
    """'1,234 tokens · about $0.04' (just the tokens when the cost isn't known)."""
    label = cost_label(dollars)
    return f"{tokens:,} tokens · {label}" if label else f"{tokens:,} tokens"
