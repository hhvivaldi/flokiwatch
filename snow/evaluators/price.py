"""Price-level evaluators: price_above, price_below.

Both use the mid-price from LiveData. Returns False if the mid-price
is unavailable (MT5 disconnected, or tick missing bid/ask).
"""
from __future__ import annotations

from snow.evaluators.context import EvalContext
from snow.schema import PriceAbove, PriceBelow


def evaluate_price_above(cond: PriceAbove, ctx: EvalContext) -> bool:
    price = ctx.live_data.price("mid")
    if price is None:
        return False
    return price > cond.level


def evaluate_price_below(cond: PriceBelow, ctx: EvalContext) -> bool:
    price = ctx.live_data.price("mid")
    if price is None:
        return False
    return price < cond.level
