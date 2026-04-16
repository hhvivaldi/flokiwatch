"""
Chart pattern detection — double top/bottom, head & shoulders, wedges, channels,
failed breakouts. Extracted from main.py (FLO-290 commit 5) so Floki can request
algorithmic pattern detection as a tool instead of receiving it via auto-context.

Input: list of bars (MT5 rate tuples OR dicts with o/h/l/c/time).
Output: list of pattern dicts — each with type, description, and metadata.
"""

from datetime import datetime
from typing import Dict, List, Optional, Any


def _bars_to_arrays(bars: Any) -> Optional[Dict[str, list]]:
    """Normalize bars (MT5 tuples or dicts) into highs/lows/closes/times arrays."""
    try:
        if bars is None or len(bars) < 10:
            return None
        highs: list = []
        lows: list = []
        closes: list = []
        times: list = []
        for b in bars:
            if isinstance(b, dict):
                highs.append(float(b.get("high", b.get("h"))))
                lows.append(float(b.get("low", b.get("l"))))
                closes.append(float(b.get("close", b.get("c"))))
                t = b.get("time") or b.get("t") or 0
                if isinstance(t, (int, float)):
                    times.append(datetime.fromtimestamp(int(t)))
                else:
                    times.append(datetime.utcnow())
            else:
                highs.append(float(b[2]))
                lows.append(float(b[3]))
                closes.append(float(b[4]))
                times.append(datetime.fromtimestamp(int(b[0])))
        return {"highs": highs, "lows": lows, "closes": closes, "times": times}
    except Exception:
        return None


def detect_patterns(h4_bars: Any, current_price: float) -> List[Dict[str, Any]]:
    """
    Detect chart patterns on H4 bars relative to current price.

    Parameters
    ----------
    h4_bars : MT5 rates array or list of dicts (o/h/l/c/time)
    current_price : float

    Returns
    -------
    List of pattern dicts. Each dict has:
      type        : "double_top" | "double_bottom" | "failed_breakout" |
                    "head_and_shoulders" | "rising_wedge" | "falling_wedge" |
                    "channel"
      description : human-readable summary
      bias        : "bullish" | "bearish" | "neutral"
      price       : central price level (float, or None)
    """
    arrs = _bars_to_arrays(h4_bars)
    if not arrs:
        return []

    highs = arrs["highs"]
    lows = arrs["lows"]
    closes = arrs["closes"]
    times = arrs["times"]
    n_bars = len(highs)
    _cp = float(current_price)

    swing_highs: list = []
    swing_lows: list = []
    for i in range(2, n_bars - 2):
        if highs[i] >= max(highs[i - 2:i]) and highs[i] >= max(highs[i + 1:i + 3]):
            swing_highs.append((i, highs[i]))
        if lows[i] <= min(lows[i - 2:i]) and lows[i] <= min(lows[i + 1:i + 3]):
            swing_lows.append((i, lows[i]))

    out: List[Dict[str, Any]] = []

    for a in range(len(swing_highs)):
        for b_idx in range(a + 1, len(swing_highs)):
            ai, ap = swing_highs[a]
            bi, bp = swing_highs[b_idx]
            if bi - ai >= 3 and abs(ap - bp) <= 50:
                avg_top = round((ap + bp) / 2)
                dist = round(avg_top - _cp)
                if 0 < dist < 200:
                    out.append({
                        "type": "double_top",
                        "bias": "bearish",
                        "price": float(avg_top),
                        "description": (
                            f"Double top forming at ${avg_top} "
                            f"(swing highs ${round(ap)} + ${round(bp)}, +{dist} from price)"
                        ),
                    })

    for a in range(len(swing_lows)):
        for b_idx in range(a + 1, len(swing_lows)):
            ai, ap = swing_lows[a]
            bi, bp = swing_lows[b_idx]
            if bi - ai >= 3 and abs(ap - bp) <= 50:
                avg_bot = round((ap + bp) / 2)
                dist = round(_cp - avg_bot)
                if 0 < dist < 200:
                    out.append({
                        "type": "double_bottom",
                        "bias": "bullish",
                        "price": float(avg_bot),
                        "description": (
                            f"Double bottom forming at ${avg_bot} "
                            f"(swing lows ${round(ap)} + ${round(bp)}, -{dist} below price)"
                        ),
                    })

    for si, sp in swing_highs:
        for j in range(si + 1, min(si + 6, n_bars)):
            if closes[j] > sp:
                for k in range(j + 1, min(j + 4, n_bars)):
                    if closes[k] < sp:
                        t_break = times[j].strftime("%b%d %H:%M")
                        t_fail = times[k].strftime("%b%d %H:%M")
                        out.append({
                            "type": "failed_breakout",
                            "bias": "bearish",
                            "price": float(round(sp)),
                            "description": (
                                f"Failed breakout at ${round(sp)} "
                                f"(broke above at {t_break}, failed back below by {t_fail})"
                            ),
                        })
                        break
                break

    try:
        for a in range(len(swing_highs) - 2):
            ai, ap = swing_highs[a]
            bi, bp = swing_highs[a + 1]
            ci, cp = swing_highs[a + 2]
            if bp > ap and bp > cp and bi - ai >= 3 and ci - bi >= 3:
                neck_lows = [lp for li, lp in swing_lows if ai < li < ci]
                if len(neck_lows) >= 2:
                    neckline = round(sum(neck_lows[:2]) / 2)
                    if abs(neck_lows[0] - neck_lows[1]) <= 50:
                        dist = round(_cp - neckline)
                        if 0 < dist < 150:
                            out.append({
                                "type": "head_and_shoulders",
                                "bias": "bearish",
                                "price": float(neckline),
                                "description": (
                                    f"H&S forming, neckline at ${neckline} "
                                    f"(head ${round(bp)}, shoulders ${round(ap)} + ${round(cp)}, "
                                    f"price {dist} above neckline)"
                                ),
                            })
    except Exception:
        pass

    try:
        if len(swing_highs) >= 3 and len(swing_lows) >= 3:
            sh3 = swing_highs[-3:]
            sl3 = swing_lows[-3:]
            hh_rising = sh3[0][1] < sh3[1][1] < sh3[2][1]
            hl_rising = sl3[0][1] < sl3[1][1] < sl3[2][1]
            range_first = sh3[0][1] - sl3[0][1]
            range_last = sh3[2][1] - sl3[2][1]
            if hh_rising and hl_rising and range_first > 0 and range_last < range_first * 0.8:
                out.append({
                    "type": "rising_wedge",
                    "bias": "bearish",
                    "price": float(round((sh3[2][1] + sl3[2][1]) / 2)),
                    "description": (
                        f"Rising wedge forming between ${round(sl3[2][1])}-${round(sh3[2][1])} "
                        f"(bearish, range narrowing {round(range_first)}->{round(range_last)} pips)"
                    ),
                })
    except Exception:
        pass

    try:
        if len(swing_highs) >= 3 and len(swing_lows) >= 3:
            sh3 = swing_highs[-3:]
            sl3 = swing_lows[-3:]
            lh_falling = sh3[0][1] > sh3[1][1] > sh3[2][1]
            ll_falling = sl3[0][1] > sl3[1][1] > sl3[2][1]
            range_first = sh3[0][1] - sl3[0][1]
            range_last = sh3[2][1] - sl3[2][1]
            if lh_falling and ll_falling and range_first > 0 and range_last < range_first * 0.8:
                out.append({
                    "type": "falling_wedge",
                    "bias": "bullish",
                    "price": float(round((sh3[2][1] + sl3[2][1]) / 2)),
                    "description": (
                        f"Falling wedge forming between ${round(sl3[2][1])}-${round(sh3[2][1])} "
                        f"(bullish, range narrowing {round(range_first)}->{round(range_last)} pips)"
                    ),
                })
    except Exception:
        pass

    try:
        if len(swing_highs) >= 2 and len(swing_lows) >= 2:
            sh_prices = [p for _, p in swing_highs[-4:]]
            sl_prices = [p for _, p in swing_lows[-4:]]
            sh_range = max(sh_prices) - min(sh_prices)
            sl_range = max(sl_prices) - min(sl_prices)
            if sh_range <= 50 and sl_range <= 50:
                ch_top = round(sum(sh_prices) / len(sh_prices))
                ch_bot = round(sum(sl_prices) / len(sl_prices))
                if ch_top - ch_bot > 20:
                    out.append({
                        "type": "channel",
                        "bias": "neutral",
                        "price": float(round((ch_top + ch_bot) / 2)),
                        "description": (
                            f"Channel ${ch_bot}-${ch_top} "
                            f"({len(sh_prices)} highs within {round(sh_range)} pips, "
                            f"{len(sl_prices)} lows within {round(sl_range)} pips)"
                        ),
                    })
    except Exception:
        pass

    seen: set = set()
    unique: List[Dict[str, Any]] = []
    for p in out:
        key = p["description"][:30]
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique[:6]
