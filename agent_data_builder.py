"""
AGENT DATA BUILDER
Builds the data package sent to the AI Agent.
Collects raw price data, indicators, Brain analysis, ML predictions,
news/macro data, positions, and session context.
"""

import json
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any

from logger import log

logger = log


def load_session_memory(path: str = "data/agent_session_memory.json") -> Optional[Dict[str, Any]]:
    try:
        abs_path = os.path.abspath(path)
        if not os.path.exists(abs_path):
            return None
        with open(abs_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        return payload if isinstance(payload, dict) else None
    except Exception as e:
        logger.debug(f"Session memory load failed (non-blocking): {e}")
        return None


def format_session_memory_xml(memory: Optional[Dict[str, Any]]) -> str:
    mem = memory if isinstance(memory, dict) else {}
    if not mem:
        return "<session_memory/>"

    thesis = mem.get("thesis")
    trades_today = mem.get("trades_today")
    wins_today = mem.get("wins_today")
    losses_today = mem.get("losses_today")
    notes = mem.get("notes") if isinstance(mem.get("notes"), list) else []

    lines: List[str] = []
    lines.append("<session_memory>")
    if thesis is not None:
        lines.append(f"  <thesis>{_xml_escape(thesis)}</thesis>")
    if trades_today is not None:
        lines.append(f"  <trades_today>{_xml_escape(trades_today)}</trades_today>")
    if wins_today is not None:
        lines.append(f"  <wins_today>{_xml_escape(wins_today)}</wins_today>")
    if losses_today is not None:
        lines.append(f"  <losses_today>{_xml_escape(losses_today)}</losses_today>")

    lines.append("  <notes>")
    for n in notes[:10]:
        if not isinstance(n, dict):
            continue
        t = n.get("time")
        note = n.get("note")
        if note is None:
            continue
        lines.append(f"    <note time=\"{_xml_attr(t)}\">{_xml_escape(note)}</note>")
    lines.append("  </notes>")
    lines.append("</session_memory>")
    return "\n".join(lines)


def _xml_escape(text: Any) -> str:
    """Escape text content for safe inclusion in XML."""
    if text is None:
        return ""
    s = str(text)
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _xml_attr(value: Any) -> str:
    """Format an attribute value (escaped) as string."""
    if value is None:
        return ""
    return _xml_escape(value)


def _normalize_event_name(name: Any) -> str:
    try:
        return " ".join(str(name or "").strip().lower().split())
    except Exception:
        return ""


def _as_optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    if value == "":
        return None
    try:
        # Preserve numeric formatting without trailing .0 if it was int-like
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            if isinstance(value, float) and value.is_integer():
                return str(int(value))
            return str(value)
        s = str(value).strip()
        return s if s else None
    except Exception:
        return None


def _csv_candle_rows(candles: List[Dict]) -> str:
    """Render candles as compact CSV-style rows: time, o, h, l, c, v."""
    if not candles:
        return ""
    lines: List[str] = []
    for c in candles:
        t = c.get("time", "")
        o = c.get("o", 0)
        h = c.get("h", 0)
        l = c.get("l", 0)
        cl = c.get("c", 0)
        v = c.get("v", 0)
        lines.append(f"{t}, {o}, {h}, {l}, {cl}, {v}")
    return "\n".join(lines)


def format_fast_xml(
    trigger_type: str,
    trigger_data: Dict[str, Any],
    current_price: Dict[str, Any],
    atr_points: Optional[float],
    m5_candles: List[Dict[str, Any]],
    positions: List[Dict[str, Any]],
    upcoming_events: List[Dict[str, Any]],
) -> str:
    lines: List[str] = []
    tt = str(trigger_type or "").strip()
    td = trigger_data if isinstance(trigger_data, dict) else {}
    cp = current_price if isinstance(current_price, dict) else {}

    lines.append("## FAST DECISION — MONITOR TRIGGER")
    lines.append("You are receiving a compact snapshot because a 1-minute monitor trigger fired.")
    lines.append("Respond using the FAST_DECISION schema in your system prompt.")
    lines.append("")

    lines.append(f"<snapshot_time>{_xml_escape(datetime.utcnow().isoformat())}</snapshot_time>")
    lines.append("")

    lines.append(
        f"<current_price bid=\"{_xml_attr(cp.get('bid'))}\" ask=\"{_xml_attr(cp.get('ask'))}\" spread=\"{_xml_attr(cp.get('spread'))}\"/>")
    lines.append("")

    atr_v = None
    try:
        atr_v = float(atr_points) if atr_points is not None else None
    except Exception:
        atr_v = None
    if atr_v is not None:
        lines.append(f"<atr value=\"{_xml_attr(round(atr_v, 2))}\" unit=\"points\"/>")
        lines.append("")

    lines.append(f"<trigger type=\"{_xml_attr(tt)}\">")
    for k, v in td.items():
        lines.append(f"  <field name=\"{_xml_attr(k)}\">{_xml_escape(v)}</field>")
    lines.append("</trigger>")
    lines.append("")

    lines.append(f"<m5_candles count=\"{_xml_attr(len(m5_candles or []))}\" description=\"Last 10 M5 candles\">")
    rows = _csv_candle_rows(m5_candles or [])
    if rows:
        for r in rows.splitlines():
            lines.append(f"  {r}")
    lines.append("</m5_candles>")
    lines.append("")

    pos_list = positions if isinstance(positions, list) else []
    lines.append(f"<positions count=\"{_xml_attr(len(pos_list))}\">")
    for p in pos_list:
        if not isinstance(p, dict):
            continue
        lines.append(
            "  <position "
            f"ticket=\"{_xml_attr(p.get('ticket'))}\" "
            f"direction=\"{_xml_attr(p.get('direction'))}\" "
            f"open=\"{_xml_attr(p.get('open_price'))}\" "
            f"current=\"{_xml_attr(p.get('current_price'))}\" "
            f"sl=\"{_xml_attr(p.get('sl'))}\" "
            f"phase=\"{_xml_attr(p.get('phase'))}\" "
            f"current_sl=\"{_xml_attr(p.get('sl'))}\" "
            f"tp=\"{_xml_attr(p.get('tp'))}\" "
            f"profit_pips=\"{_xml_attr(p.get('profit_pips'))}\" "
            f"profit=\"{_xml_attr(p.get('profit'))}\"/>")
    lines.append("</positions>")
    lines.append("")

    # Active trade context for FAST decisions (use first position snapshot)
    try:
        if pos_list and isinstance(pos_list[0], dict):
            p0 = pos_list[0]
            lines.append(
                f"<active_trade_context phase=\"{_xml_attr(p0.get('phase'))}\" current_sl=\"{_xml_attr(p0.get('sl'))}\"/>"
            )
            lines.append("")
    except Exception:
        pass

    evs = upcoming_events if isinstance(upcoming_events, list) else []
    lines.append(f"<upcoming_events count=\"{_xml_attr(len(evs))}\">")
    for ev in evs:
        if not isinstance(ev, dict):
            continue
        lines.append(
            "  <event "
            f"name=\"{_xml_attr(ev.get('name'))}\" "
            f"time=\"{_xml_attr(ev.get('time'))}\" "
            f"importance=\"{_xml_attr(ev.get('importance'))}\" "
            f"minutes_until=\"{_xml_attr(ev.get('minutes_until'))}\"/>"
        )
    lines.append("</upcoming_events>")
    lines.append("")

    lines.append("<checklist_reminder>")
    lines.append("Complete your data_checklist with specific values from the data above. Every field is mandatory. Reference at least 8 categories in your reasoning.")
    lines.append("</checklist_reminder>")
    lines.append("")

    return "\n".join(lines)


def _format_price_2dp(value: Any) -> str:
    try:
        return f"{float(value):.2f}"
    except Exception:
        return ""


def _compute_fibonacci_from_h1(h1_candles: List[Dict]) -> Optional[Dict[str, Any]]:
    if not h1_candles or len(h1_candles) < 2:
        return None

    swing_high = None
    swing_low = None
    idx_high = -1
    idx_low = -1

    for i, c in enumerate(h1_candles):
        try:
            hi = float(c.get("h"))
            lo = float(c.get("l"))
        except Exception:
            continue

        if swing_high is None or hi > swing_high:
            swing_high = hi
            idx_high = i
        if swing_low is None or lo < swing_low:
            swing_low = lo
            idx_low = i

    if swing_high is None or swing_low is None:
        return None
    if swing_high <= swing_low:
        return None

    direction = "up" if idx_low < idx_high else "down"
    rng = swing_high - swing_low

    levels_pct = [23.6, 38.2, 50.0, 61.8, 78.6]
    levels: List[Dict[str, Any]] = []
    for pct in levels_pct:
        r = pct / 100.0
        if direction == "up":
            price = swing_high - rng * r
        else:
            price = swing_low + rng * r
        levels.append({"pct": f"{pct:.1f}", "price": _format_price_2dp(price)})

    return {
        "swing_low": _format_price_2dp(swing_low),
        "swing_high": _format_price_2dp(swing_high),
        "direction": direction,
        "levels": levels,
    }


def _compute_swing_points_h1(h1_candles: List[Dict], n: int = 3, max_points: int = 5) -> Dict[str, Any]:
    highs: List[Dict[str, Any]] = []
    lows: List[Dict[str, Any]] = []

    if not h1_candles or len(h1_candles) < (2 * n + 1):
        return {
            "swing_highs": [],
            "swing_lows": [],
            "structure": "",
        }

    start = n
    end = len(h1_candles) - n
    for i in range(start, end):
        c = h1_candles[i]
        try:
            hi = float(c.get("h"))
            lo = float(c.get("l"))
        except Exception:
            continue

        prev = h1_candles[i - n:i]
        nxt = h1_candles[i + 1:i + 1 + n]
        try:
            prev_highs = [float(x.get("h")) for x in prev]
            next_highs = [float(x.get("h")) for x in nxt]
            prev_lows = [float(x.get("l")) for x in prev]
            next_lows = [float(x.get("l")) for x in nxt]
        except Exception:
            continue

        if prev_highs and next_highs and hi > max(prev_highs) and hi > max(next_highs):
            highs.append({"price": _format_price_2dp(hi), "time": c.get("time", "")})

        if prev_lows and next_lows and lo < min(prev_lows) and lo < min(next_lows):
            lows.append({"price": _format_price_2dp(lo), "time": c.get("time", "")})

    highs_newest = list(reversed(highs))[:max_points]
    lows_newest = list(reversed(lows))[:max_points]

    structure = ""
    try:
        has_2h = len(highs_newest) >= 2
        has_2l = len(lows_newest) >= 2

        if has_2h and has_2l:
            h0 = float(highs_newest[0]["price"])
            h1p = float(highs_newest[1]["price"])
            l0 = float(lows_newest[0]["price"])
            l1p = float(lows_newest[1]["price"])
            if h0 > h1p and l0 > l1p:
                structure = "higher highs and higher lows"
            elif h0 < h1p and l0 < l1p:
                structure = "lower highs and lower lows"
            else:
                structure = "mixed — no clear trend"
        elif has_2h and not has_2l:
            h0 = float(highs_newest[0]["price"])
            h1p = float(highs_newest[1]["price"])
            if h0 > h1p:
                structure = "higher highs"
            elif h0 < h1p:
                structure = "lower highs"
            else:
                structure = "mixed — no clear trend"
        elif not has_2h and has_2l:
            l0 = float(lows_newest[0]["price"])
            l1p = float(lows_newest[1]["price"])
            if l0 > l1p:
                structure = "higher lows"
            elif l0 < l1p:
                structure = "lower lows"
            else:
                structure = "mixed — no clear trend"
    except Exception:
        structure = ""

    return {
        "swing_highs": highs_newest,
        "swing_lows": lows_newest,
        "structure": structure,
    }


def _format_pct_signed(value: float) -> str:
    try:
        v = float(value)
    except Exception:
        return ""
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.2f}"


def _pct_change(current_price: float, reference_price: float) -> Optional[float]:
    try:
        cp = float(current_price)
        rp = float(reference_price)
        if rp == 0:
            return None
        return (cp - rp) / rp * 100.0
    except Exception:
        return None


def _parse_candle_dt(value: Any):
    try:
        if value is None:
            return None
        s = str(value).strip()
        if not s:
            return None
        if "T" not in s and " " in s:
            s = s.replace(" ", "T", 1)
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _find_day_open_candle(h1_candles: List[Dict], current_dt: datetime) -> Optional[Dict[str, Any]]:
    if not h1_candles or not current_dt:
        return None

    day = current_dt.date()
    for c in h1_candles:
        dt = _parse_candle_dt(c.get("time"))
        if not dt:
            continue
        if dt.date() == day:
            return c
    return None


BROKER_UTC_OFFSET_HOURS = 3

# Broker server timezone offset from UTC. Currently UTC+3 (CapitalPointTrading).
# May change with DST — verify if price change calculations break after clock changes.


def _current_session_name_broker_hour(broker_hour: int) -> str:
    try:
        h = int(broker_hour)
    except Exception:
        return "unknown"
    if h < 11:
        return "Asian"
    if h < 16:
        return "London"
    return "NY"


def _session_start_hour_from_broker_hour(broker_hour: int) -> Optional[int]:
    try:
        h = int(broker_hour)
    except Exception:
        return None
    if h < 11:
        return 3
    if h < 16:
        return 11
    return 16


def _session_start_hour_broker(session_name: str) -> Optional[int]:
    if not session_name:
        return None
    name = str(session_name).lower()
    if name == "asian":
        return 3
    if name == "london":
        return 11
    if name == "ny":
        return 16
    return None


def _find_session_open_candle(h1_candles: List[Dict], current_dt: datetime, session_name: str) -> Optional[Dict[str, Any]]:
    if not h1_candles or not current_dt:
        return None
    start_hour = _session_start_hour_broker(session_name)
    if start_hour is None:
        return None

    day = current_dt.date()
    for c in h1_candles:
        dt = _parse_candle_dt(c.get("time"))
        if not dt:
            continue
        if dt.date() == day and dt.hour >= start_hour:
            return c
    return None


def _find_session_open_candle_by_start_hour(h1_candles: List[Dict], current_dt: datetime, start_hour: int) -> Optional[Dict[str, Any]]:
    if not h1_candles or not current_dt:
        return None
    try:
        start = int(start_hour)
    except Exception:
        return None

    day = current_dt.date()
    for c in h1_candles:
        dt = _parse_candle_dt(c.get("time"))
        if not dt:
            continue
        if dt.date() == day and dt.hour >= start:
            return c
    return None


def format_proactive_xml(data_package: Dict) -> str:
    """Convert a proactive data package dict into the XML-tagged snapshot format."""
    dp = data_package or {}

    ts = dp.get("timestamp", "")
    cp = dp.get("current_price", {}) or {}

    h1 = dp.get("h1_candles", []) or []
    h4 = dp.get("h4_candles", []) or []
    d1 = dp.get("d1_candles", []) or []
    m5 = dp.get("m5_candles", []) or []

    fib = _compute_fibonacci_from_h1(h1)
    swings = _compute_swing_points_h1(h1, n=3, max_points=5)

    current_bid = None
    try:
        current_bid = float(cp.get("bid"))
    except Exception:
        current_bid = None

    current_dt = _parse_candle_dt(ts)

    price_changes: List[Dict[str, Any]] = []
    if current_bid is not None:
        if len(h1) >= 2:
            ref_1h = h1[-2].get("c")
            pct = _pct_change(current_bid, ref_1h)
            if pct is not None:
                price_changes.append({"period": "1h", "pct": _format_pct_signed(pct)})
        if len(h1) >= 5:
            ref_4h = h1[-5].get("c")
            pct = _pct_change(current_bid, ref_4h)
            if pct is not None:
                price_changes.append({"period": "4h", "pct": _format_pct_signed(pct)})
        if len(h1) >= 9:
            ref_8h = h1[-9].get("c")
            pct = _pct_change(current_bid, ref_8h)
            if pct is not None:
                price_changes.append({"period": "8h", "pct": _format_pct_signed(pct)})

        day_candle = _find_day_open_candle(h1, current_dt) if current_dt else None
        day_open = None
        if isinstance(day_candle, dict):
            day_open = day_candle.get("o")
        if day_open is not None:
            pct = _pct_change(current_bid, day_open)
            if pct is not None:
                price_changes.append({"period": "day", "pct": _format_pct_signed(pct)})

        sess_name = None
        sess_start_hour = None
        if current_dt:
            sess_name = _current_session_name_broker_hour(current_dt.hour)
            sess_start_hour = _session_start_hour_from_broker_hour(current_dt.hour)

        sess_candle = None
        if current_dt and sess_start_hour is not None:
            sess_candle = _find_session_open_candle_by_start_hour(h1, current_dt, sess_start_hour)
        sess_open = None
        if isinstance(sess_candle, dict):
            sess_open = sess_candle.get("o")
        if sess_open is not None:
            pct = _pct_change(current_bid, sess_open)
            if pct is not None:
                price_changes.append({"period": "session", "pct": _format_pct_signed(pct), "session": sess_name})

    mtf = dp.get("mtf_trend", {}) or {}
    patterns = dp.get("candlestick_patterns", {}) or {}
    macro = dp.get("macro", {}) or {}
    indicators = dp.get("indicators", {}) or {}
    ml = dp.get("ml_predictions", {}) or {}

    sr_zones = dp.get("sr_zones", []) or []
    nearest_support = dp.get("nearest_support") or {}
    nearest_resistance = dp.get("nearest_resistance") or {}
    sr_prox = dp.get("sr_proximity", {}) or {}

    vol = dp.get("volatility", {}) or {}
    session = dp.get("session", {}) or {}
    positions = dp.get("positions", []) or []
    trade_feedback = dp.get("trade_feedback", {}) or {}

    primary = patterns.get("primary_pattern")
    all_patterns_list = patterns.get("patterns", []) or []
    all_patterns_str = ", ".join([p.get("name", "") for p in all_patterns_list if isinstance(p, dict) and p.get("name")])

    headlines = macro.get("headlines", []) or []

    rsi = indicators.get("rsi", {}) or {}
    macd = indicators.get("macd", {}) or {}
    emas = indicators.get("emas", {}) or {}
    bb = indicators.get("bollinger", {}) or {}
    atr = indicators.get("atr", {}) or {}
    adx = indicators.get("adx", {}) or {}
    volume = indicators.get("volume", {}) or {}

    dxy = macro.get("dxy", {}) or {}
    vix = macro.get("vix", {}) or {}
    yields_10y = macro.get("yields_10y", {}) or {}
    calendar = macro.get("calendar", {}) or {}
    sentiment = macro.get("sentiment", {}) or {}

    ml_pred = ml.get("prediction")
    ml_conf = ml.get("confidence")
    ml_pattern = ml.get("pattern")
    ml_h1 = ml.get("h1", {}) or {}
    ml_h4 = ml.get("h4", {}) or {}
    ml_agreement = ml.get("ensemble_agreement")

    cooling_until = vol.get("cooling_until")
    cooling_until_attr = "null" if cooling_until in (None, "", "None") else str(cooling_until)

    lines: List[str] = []

    lines.append("## INDEPENDENT H1 MARKET SNAPSHOT")
    lines.append("Analyze the raw market data below. Read structure first, then macro, then indicators. What would YOU trade right now?")
    lines.append("")
    lines.append(f"<snapshot_time>{_xml_escape(ts)}</snapshot_time>")
    lines.append("")
    lines.append(
        f"<current_price bid=\"{_xml_attr(cp.get('bid'))}\" ask=\"{_xml_attr(cp.get('ask'))}\" spread=\"{_xml_attr(cp.get('spread'))}\"/>")
    lines.append("")

    try:
        ler = dp.get("last_execution_result") or {}
        if isinstance(ler, dict) and ler:
            lines.append("<last_execution_result>")
            lines.append(
                "  <rejected"
                + f" decision=\"{_xml_attr(ler.get('decision'))}\""
                + f" reason=\"{_xml_attr(ler.get('reason'))}\""
                + f" timestamp=\"{_xml_attr(ler.get('timestamp'))}\""
                + "/>"
            )
            lines.append("</last_execution_result>")
            lines.append("")
    except Exception:
        pass

    try:
        trade_history = dp.get("trade_history", []) or []
        last_trade = trade_history[0] if isinstance(trade_history, list) and trade_history else None
        if isinstance(last_trade, dict):
            direction = last_trade.get("direction")
            entry = last_trade.get("open_price")
            close = last_trade.get("close_price")
            profit_dollars = last_trade.get("profit")
            close_reason = last_trade.get("reason")
            close_time = last_trade.get("close_time")

            profit_points = None
            try:
                if entry is not None and close is not None:
                    profit_points = float(close) - float(entry)
                    if str(direction or "").upper() == "SELL":
                        profit_points = -profit_points
            except Exception:
                profit_points = None

            duration = None
            try:
                open_time = last_trade.get("open_time")
                if open_time and close_time:
                    dt_open = datetime.fromisoformat(str(open_time).replace("Z", "+00:00"))
                    dt_close = datetime.fromisoformat(str(close_time).replace("Z", "+00:00"))
                    secs = int((dt_close - dt_open).total_seconds())
                    if secs >= 0:
                        hours = secs // 3600
                        mins = (secs % 3600) // 60
                        if hours > 0:
                            duration = f"{hours}h {mins}m"
                        else:
                            duration = f"{mins}m"
            except Exception:
                duration = None

            profit_points_str = ""
            try:
                if profit_points is not None:
                    sign = "+" if float(profit_points) >= 0 else ""
                    profit_points_str = f"{sign}{float(profit_points):.2f}"
            except Exception:
                profit_points_str = ""

            profit_dollars_str = ""
            try:
                if profit_dollars is not None:
                    sign = "+" if float(profit_dollars) >= 0 else ""
                    profit_dollars_str = f"{sign}${float(profit_dollars):.2f}"
            except Exception:
                profit_dollars_str = ""

            lines.append("<last_trade_result>")
            lines.append(
                "  <trade"
                + f" direction=\"{_xml_attr(direction)}\""
                + f" entry=\"{_xml_attr(entry)}\""
                + f" close=\"{_xml_attr(close)}\""
                + f" profit_points=\"{_xml_attr(profit_points_str)}\""
                + f" profit_dollars=\"{_xml_attr(profit_dollars_str)}\""
                + f" duration=\"{_xml_attr(duration)}\""
                + f" close_reason=\"{_xml_attr(close_reason)}\""
                + f" timestamp=\"{_xml_attr(close_time)}\""
                + "/>"
            )
            lines.append("</last_trade_result>")
            lines.append("")
    except Exception:
        pass

    try:
        trade_history = dp.get("trade_history", []) or []
        if isinstance(trade_history, list) and trade_history:
            items = []
            for t in trade_history[:5]:
                if isinstance(t, dict) and (t.get("close_time") or t.get("profit") is not None or t.get("direction")):
                    items.append(t)

            if items:
                now = datetime.now(timezone.utc)
                lines.append("<recent_trade_history>")
                for t in items:
                    direction = t.get("direction")
                    profit_dollars = t.get("profit")
                    close_reason = t.get("reason")
                    close_time = t.get("close_time")

                    time_ago = ""
                    try:
                        if close_time:
                            dt_close = datetime.fromisoformat(str(close_time).replace("Z", "+00:00"))
                            if dt_close.tzinfo is None:
                                dt_close = dt_close.replace(tzinfo=timezone.utc)
                            secs = int((now - dt_close).total_seconds())
                            if secs >= 0:
                                hours = secs // 3600
                                mins = (secs % 3600) // 60
                                if hours > 0:
                                    time_ago = f"{hours}h {mins}m"
                                else:
                                    time_ago = f"{mins}m"
                    except Exception:
                        time_ago = ""

                    profit_dollars_str = ""
                    try:
                        if profit_dollars is not None:
                            sign = "+" if float(profit_dollars) >= 0 else ""
                            profit_dollars_str = f"{sign}${float(profit_dollars):.2f}"
                    except Exception:
                        profit_dollars_str = ""

                    reason_short = close_reason
                    try:
                        ct = str(t.get("close_type") or "").lower()
                        if ct == "tp":
                            reason_short = "TP"
                        elif ct == "sl":
                            reason_short = "SL"
                        elif ct == "trailing":
                            reason_short = "TRAIL"
                        elif ct == "breakeven":
                            reason_short = "BE"
                    except Exception:
                        reason_short = close_reason

                    lines.append(
                        "  <trade"
                        + f" direction=\"{_xml_attr(direction)}\""
                        + f" profit=\"{_xml_attr(profit_dollars_str)}\""
                        + f" close_reason=\"{_xml_attr(reason_short)}\""
                        + f" time_ago=\"{_xml_attr(time_ago)}\""
                        + "/>"
                    )
                lines.append("</recent_trade_history>")
                lines.append("")
    except Exception:
        pass

    recent_decisions = dp.get("recent_decisions", []) or []
    if recent_decisions:
        lines.append("--- SECTION 0: YOUR RECENT DECISIONS (Read this BEFORE anything else) ---")
        lines.append("")
        lines.append(f"<your_recent_decisions count=\"{len(recent_decisions)}\">")
        for dec in recent_decisions:
            if not isinstance(dec, dict):
                continue
            
            t = dec.get("timestamp", "")
            if t:
                try:
                    dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
                    t = dt.strftime("%H:%00 UTC")
                except Exception:
                    pass
                    
            action = dec.get("decision", "")
            conf = dec.get("confidence", "")
            
            entry = dec.get("entry")
            sl = dec.get("sl")
            tp = dec.get("tp")
            rr = dec.get("rr")
            
            attr_str = f"time=\"{_xml_attr(t)}\" action=\"{_xml_attr(action)}\" confidence=\"{_xml_attr(conf)}\""
            if action in ("OPEN_BUY", "OPEN_SELL") and entry is not None:
                attr_str += f" entry=\"{_xml_attr(entry)}\" sl=\"{_xml_attr(sl)}\" tp=\"{_xml_attr(tp)}\" rr=\"{_xml_attr(rr)}\""
                
            lines.append(f"  <decision {attr_str}/>")
        lines.append("</your_recent_decisions>")
        lines.append("")

    active_trade_context = dp.get("active_trade_context") or {}
    if isinstance(active_trade_context, dict) and active_trade_context:
        entry = active_trade_context.get("entry") or {}
        at_phase = active_trade_context.get("phase")
        at_sl = active_trade_context.get("current_sl")
        lines.append(
            f"<active_trade_context phase=\"{_xml_attr(at_phase)}\" current_sl=\"{_xml_attr(at_sl)}\">"
        )
        lines.append(
            "  <entry direction=\"" + _xml_attr(entry.get("direction")) + "\" price=\"" + _xml_attr(entry.get("price")) + "\" timestamp=\"" + _xml_attr(entry.get("timestamp")) + "\"/>"
        )
        lines.append(f"  <current_price>{_xml_escape(active_trade_context.get('current_price'))}</current_price>")
        lines.append(f"  <pnl_points>{_xml_escape(active_trade_context.get('pnl_points'))}</pnl_points>")
        lines.append(f"  <pnl_status>{_xml_escape(active_trade_context.get('pnl_status'))}</pnl_status>")
        lines.append(f"  <distance_to_sl>{_xml_escape(active_trade_context.get('distance_to_sl'))}</distance_to_sl>")
        lines.append(f"  <distance_to_tp>{_xml_escape(active_trade_context.get('distance_to_tp'))}</distance_to_tp>")
        lines.append(f"  <sl>{_xml_escape(active_trade_context.get('sl'))}</sl>")
        lines.append(f"  <tp>{_xml_escape(active_trade_context.get('tp'))}</tp>")
        lines.append("</active_trade_context>")
        lines.append("")

    lines.append("--- SECTION 1: PRICE STRUCTURE (Read this FIRST) ---")
    lines.append("")
    lines.append("<price_structure>")
    lines.append("  <h1_candles count=\"50\" description=\"Last 50 hourly candles, newest last\">")
    rows = _csv_candle_rows(h1)
    if rows:
        for r in rows.splitlines():
            lines.append(f"    {r}")
    lines.append("  </h1_candles>")
    lines.append("")
    lines.append("  <h4_candles count=\"20\" description=\"Last 20 four-hour candles, newest last\">")
    rows = _csv_candle_rows(h4)
    if rows:
        for r in rows.splitlines():
            lines.append(f"    {r}")
    lines.append("  </h4_candles>")
    lines.append("")
    lines.append("  <d1_candles count=\"10\" description=\"Last 10 daily candles, newest last\">")
    rows = _csv_candle_rows(d1)
    if rows:
        for r in rows.splitlines():
            lines.append(f"    {r}")
    lines.append("  </d1_candles>")
    lines.append("")
    lines.append("  <m5_candles count=\"10\" description=\"Last 10 five-minute candles for micro-structure\">")
    rows = _csv_candle_rows(m5)
    if rows:
        for r in rows.splitlines():
            lines.append(f"    {r}")
    lines.append("  </m5_candles>")
    lines.append("")

    if fib:
        lines.append(
            f"  <fibonacci swing_low=\"{_xml_attr(fib.get('swing_low'))}\" swing_high=\"{_xml_attr(fib.get('swing_high'))}\" direction=\"{_xml_attr(fib.get('direction'))}\">"
        )
        for lvl in fib.get("levels") or []:
            if not isinstance(lvl, dict):
                continue
            lines.append(
                f"    <level pct=\"{_xml_attr(lvl.get('pct'))}\" price=\"{_xml_attr(lvl.get('price'))}\"/>"
            )
        lines.append("  </fibonacci>")
        lines.append("")

    try:
        sh = swings.get("swing_highs") or []
        sl = swings.get("swing_lows") or []
        sh_text = ", ".join([f"{x.get('price')} ({x.get('time')})" for x in sh if isinstance(x, dict)])
        sl_text = ", ".join([f"{x.get('price')} ({x.get('time')})" for x in sl if isinstance(x, dict)])
        lines.append("  <swing_points>")
        lines.append(f"    <swing_highs>{_xml_escape(sh_text)}</swing_highs>")
        lines.append(f"    <swing_lows>{_xml_escape(sl_text)}</swing_lows>")
        lines.append(f"    <structure>{_xml_escape(swings.get('structure'))}</structure>")
        lines.append("  </swing_points>")
        lines.append("")
    except Exception:
        pass

    if price_changes:
        lines.append("  <price_changes>")
        for ch in price_changes:
            if not isinstance(ch, dict):
                continue
            period = ch.get("period")
            pct = ch.get("pct")
            if not period or pct is None:
                continue
            if period == "session":
                lines.append(
                    f"    <change period=\"session\" pct=\"{_xml_attr(pct)}\" session=\"{_xml_attr(ch.get('session'))}\"/>"
                )
            else:
                lines.append(
                    f"    <change period=\"{_xml_attr(period)}\" pct=\"{_xml_attr(pct)}\"/>"
                )
        lines.append("  </price_changes>")
        lines.append("")

    lines.append(
        f"  <mtf_trend d1=\"{_xml_attr(mtf.get('d1_direction'))}\" h4=\"{_xml_attr(mtf.get('h4_direction'))}\"/>")
    lines.append("")

    lines.append("  <candlestick_pattern>")
    if isinstance(primary, dict) and primary:
        lines.append(
            "    <primary "
            f"name=\"{_xml_attr(primary.get('name'))}\" "
            f"sr_multiplier=\"{_xml_attr(primary.get('sr_multiplier'))}\"/>"
        )
    else:
        lines.append("    <primary name=\"\" sr_multiplier=\"1.00\"/>")
    lines.append(f"    <all_patterns>{_xml_escape(all_patterns_str)}</all_patterns>")
    lines.append(f"    <sr_context>{_xml_escape(patterns.get('sr_context'))}</sr_context>")
    lines.append("  </candlestick_pattern>")
    lines.append("")

    lines.append("  <support_resistance>")
    lines.append(
        f"    <nearest_support level=\"{_xml_attr(nearest_support.get('level'))}\" distance_pips=\"{_xml_attr(nearest_support.get('distance_pips'))}\"/>"
    )
    lines.append(
        f"    <nearest_resistance level=\"{_xml_attr(nearest_resistance.get('level'))}\" distance_pips=\"{_xml_attr(nearest_resistance.get('distance_pips'))}\"/>"
    )

    nearest_info = sr_prox.get("nearest_zone_info") or {}
    nearest_info_str = ""
    if isinstance(nearest_info, dict) and nearest_info.get("price"):
        tz = nearest_info.get("timeframe")
        nearest_info_str = f"{nearest_info.get('zone_type')} at {nearest_info.get('price')}" + (f" ({tz})" if tz else "")
    elif sr_prox.get("nearest_zone_info"):
        nearest_info_str = str(sr_prox.get("nearest_zone_info"))

    lines.append(
        "    <proximity "
        f"near_strong_zone=\"{_xml_attr(sr_prox.get('near_strong_zone', False)).lower() if isinstance(sr_prox.get('near_strong_zone', False), bool) else _xml_attr(sr_prox.get('near_strong_zone'))}\" "
        f"nearest_dist_pips=\"{_xml_attr(sr_prox.get('nearest_zone_dist_pips'))}\" "
        f"nearest_info=\"{_xml_attr(nearest_info_str)}\"/>"
    )

    lines.append(f"    <zones count=\"{_xml_attr(len(sr_zones))}\">")
    for z in sr_zones:
        if not isinstance(z, dict):
            continue
        confluence_val = z.get("confluence")
        confluence_str = "false"
        if isinstance(confluence_val, list):
            confluence_str = "true" if len(confluence_val) > 0 else "false"
        elif isinstance(confluence_val, bool):
            confluence_str = "true" if confluence_val else "false"
        lines.append(
            "      <zone "
            f"price=\"{_xml_attr(z.get('price'))}\" "
            f"type=\"{_xml_attr(z.get('zone_type'))}\" "
            f"touches=\"{_xml_attr(z.get('touches'))}\" "
            f"timeframe=\"{_xml_attr(z.get('timeframe'))}\" "
            f"strength=\"{_xml_attr(z.get('strength'))}\" "
            f"dist_pips=\"{_xml_attr(z.get('dist_pips'))}\" "
            f"position=\"{_xml_attr(z.get('position'))}\" "
            f"confluence=\"{_xml_attr(confluence_str)}\"/>"
        )
    lines.append("    </zones>")
    lines.append("  </support_resistance>")
    lines.append("</price_structure>")
    lines.append("")

    lines.append("--- SECTION 2: MACRO CONTEXT (Read this SECOND) ---")
    lines.append("")
    lines.append("<macro_context>")
    lines.append(
        "  <dxy "
        f"value=\"{_xml_attr(dxy.get('value'))}\" "
        f"change_pct=\"{_xml_attr(dxy.get('change_24h'))}\" "
        f"trend=\"{_xml_attr(dxy.get('trend'))}\"/>"
    )
    lines.append(
        f"  <vix value=\"{_xml_attr(vix.get('value'))}\" change_pct=\"{_xml_attr(vix.get('change_pct'))}\"/>"
    )
    lines.append(
        f"  <yields_10y value=\"{_xml_attr(yields_10y.get('value'))}\" change_pct=\"{_xml_attr(yields_10y.get('change_pct'))}\"/>"
    )

    lines.append(
        "  <calendar "
        f"phase=\"{_xml_attr(calendar.get('phase'))}\" "
        f"source=\"{_xml_attr(calendar.get('source', 'mt5_bridge'))}\">"
    )

    upcoming_events = calendar.get("upcoming_events") or []
    if isinstance(upcoming_events, list) and upcoming_events:
        lines.append(f"    <upcoming_events count=\"{_xml_attr(len(upcoming_events))}\">")
        for ev in upcoming_events[:5]:
            if not isinstance(ev, dict):
                continue

            name = ev.get("name")
            time_val = ev.get("time")
            importance = ev.get("importance")
            time_until = ev.get("time_until")

            attrs = [
                f"name=\"{_xml_attr(name)}\"",
                f"time=\"{_xml_attr(time_val)}\"",
                f"importance=\"{_xml_attr(importance)}\"",
                f"time_until=\"{_xml_attr(time_until)}\"",
            ]

            forecast = _as_optional_str(ev.get("forecast"))
            previous = _as_optional_str(ev.get("previous"))
            if forecast is not None:
                attrs.append(f"forecast=\"{_xml_attr(forecast)}\"")
            if previous is not None:
                attrs.append(f"previous=\"{_xml_attr(previous)}\"")

            lines.append(f"      <event {' '.join(attrs)}/>")

        lines.append("    </upcoming_events>")

    lines.append("  </calendar>")

    # Headlines: 5 most recent, full text (title + optional description)
    try:
        parsed_headlines: List[Dict[str, Any]] = []
        if isinstance(headlines, list):
            for h in headlines:
                if not isinstance(h, dict):
                    continue

                ts_val = h.get("timestamp") or h.get("time")
                dt_val = None
                try:
                    if ts_val:
                        dt_val = datetime.fromisoformat(str(ts_val).replace("Z", "+00:00"))
                        if dt_val.tzinfo is None:
                            dt_val = dt_val.replace(tzinfo=timezone.utc)
                except Exception:
                    dt_val = None

                title = h.get("title") or h.get("headline") or h.get("text") or ""
                desc = h.get("description") or ""
                full_text = str(title).strip()
                if desc:
                    full_text = f"{full_text} — {str(desc).strip()}"

                parsed_headlines.append(
                    {
                        "dt": dt_val,
                        "time_attr": ts_val,
                        "text": full_text,
                    }
                )

        parsed_headlines.sort(key=lambda x: x.get("dt") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        top5 = parsed_headlines[:5]

        lines.append("  <headlines>")
        for h in top5:
            t_attr = ""
            try:
                dt_val = h.get("dt")
                if isinstance(dt_val, datetime):
                    t_attr = dt_val.strftime("%H:%M UTC")
                else:
                    t_attr = str(h.get("time_attr") or "")
            except Exception:
                t_attr = str(h.get("time_attr") or "")

            lines.append(
                f"    <headline time=\"{_xml_attr(t_attr)}\" text=\"{_xml_attr(h.get('text'))}\"/>"
            )
        lines.append("  </headlines>")
    except Exception:
        lines.append("  <headlines>")
        lines.append("  </headlines>")

    lines.append("</macro_context>")
    lines.append("")

    lines.append("--- SECTION 3: TECHNICAL INDICATORS (Read this THIRD — adjust confidence, don't decide direction) ---")
    lines.append("")

    lines.append("<indicators>")
    lines.append(
        f"  <rsi value=\"{_xml_attr(rsi.get('value'))}\"/>"
    )
    lines.append(
        "  <macd "
        f"value=\"{_xml_attr(macd.get('value'))}\" "
        f"signal=\"{_xml_attr(macd.get('signal'))}\" "
        f"histogram=\"{_xml_attr(macd.get('histogram'))}\"/>"
    )
    lines.append(
        "  <emas "
        f"ema9=\"{_xml_attr(emas.get('ema9'))}\" "
        f"ema21=\"{_xml_attr(emas.get('ema21'))}\" "
        f"ema50=\"{_xml_attr(emas.get('ema50'))}\" "
        f"price_vs_ema50_pct=\"{_xml_attr(emas.get('price_vs_ema50_pct'))}\"/>"
    )
    try:
        ema200_val = emas.get("ema200")
        cp_bid = cp.get("bid")
        pct_vs = ""
        if ema200_val not in (None, "") and cp_bid not in (None, ""):
            pct = _pct_change(float(cp_bid), float(ema200_val))
            if pct is not None:
                pct_vs = _format_pct_signed(pct) + "%"
        if ema200_val not in (None, ""):
            lines.append(
                f"  <ema200 value=\"{_xml_attr(_format_price_2dp(ema200_val))}\" price_vs_ema200_pct=\"{_xml_attr(pct_vs)}\"/>"
            )
    except Exception:
        pass
    lines.append(
        "  <bollinger "
        f"upper=\"{_xml_attr(bb.get('upper'))}\" "
        f"middle=\"{_xml_attr(bb.get('middle'))}\" "
        f"lower=\"{_xml_attr(bb.get('lower'))}\" "
        f"position=\"{_xml_attr(bb.get('position'))}\"/>"
    )
    lines.append(
        f"  <atr value=\"{_xml_attr(atr.get('value'))}\" description=\"Average True Range H1\"/>"
    )
    lines.append(
        "  <adx "
        f"value=\"{_xml_attr(adx.get('value'))}\" "
        f"plus_di=\"{_xml_attr(adx.get('plus_di'))}\" "
        f"minus_di=\"{_xml_attr(adx.get('minus_di'))}\"/>"
    )
    lines.append(
        "  <volume "
        f"ratio=\"{_xml_attr(volume.get('ratio', volume.get('tick_volume_ratio')))}\"/>"
    )
    lines.append("</indicators>")
    lines.append("")

    lines.append("--- SECTION 4: TRADING CONTEXT ---")
    lines.append("")

    lines.append(
        "<volatility "
        f"status=\"{_xml_attr(vol.get('status'))}\" "
        f"m5_move_pct=\"{_xml_attr(vol.get('m5_move_pct'))}\" "
        f"cooling_until=\"{_xml_attr(cooling_until_attr)}\"/>"
    )
    lines.append("")

    lines.append(f"<session name=\"{_xml_attr(session.get('name'))}\" hour_utc=\"{_xml_attr(session.get('hour_utc'))}\">")
    lines.append(
        "  <today "
        f"trades=\"{_xml_attr(session.get('today_trades'))}\" "
        f"wins=\"{_xml_attr(session.get('today_wins'))}\" "
        f"losses=\"{_xml_attr(session.get('today_losses'))}\" "
        f"pnl=\"{_xml_attr(session.get('today_pnl'))}\"/>"
    )
    lines.append(f"  <last_5_results>{_xml_escape(', '.join([str(x) for x in (session.get('last_5_results') or [])]))}</last_5_results>")
    lines.append(f"  <consecutive_losses>{_xml_escape(session.get('consecutive_losses'))}</consecutive_losses>")
    lines.append("</session>")
    lines.append("")

    lines.append(f"<open_positions count=\"{_xml_attr(len(positions))}\"/>")
    lines.append("")

    lines.append("<trade_feedback>")
    lines.append("  <last_trades>")
    for t in (trade_feedback.get("last_trades") or [])[:5]:
        lines.append(f"    <trade>{_xml_escape(t)}</trade>")
    lines.append("  </last_trades>")
    lines.append("  <agent_accuracy>")
    acc = trade_feedback.get("agent_accuracy") or {}
    if isinstance(acc, dict):
        for k, v in acc.items():
            lines.append(f"    <{_xml_escape(k)}>{_xml_escape(v)}</{_xml_escape(k)}>")
    lines.append("  </agent_accuracy>")
    lines.append("</trade_feedback>")
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("Based on this data: What does the price structure tell you? What is your decision? Respond with valid JSON (OPEN_BUY, OPEN_SELL, or WAIT only).")

    return "\n".join(lines)


def _safe_round(value, decimals: int = 2):
    """Safely round a value, handling strings and None."""
    if value is None:
        return 0
    try:
        return round(float(value), decimals)
    except (ValueError, TypeError):
        return 0


def build_data_package(
    brain_result: Any,
    tech_data: Dict,
    ml_data: Dict,
    momentum_data: Dict,
    news_data: Dict,
    calendar_data: Dict,
    h1_candles: List[Dict],
    m5_candles: List[Dict],
    current_price: Dict,
    positions: List[Dict],
    session_context: Dict,
    volatility_status: Dict,
    sr_zones: Optional[List] = None,
    candlestick_patterns: Optional[Dict] = None,
    sr_proximity: Optional[Dict] = None,
    d1_candles: Optional[List[Dict]] = None,
    h4_candles: Optional[List[Dict]] = None,
    agent_memory: Optional[List[Dict]] = None,
    trade_feedback: Optional[Dict] = None,
    delta_context: Optional[Dict] = None,
    portfolio: Optional[Dict] = None,
    regime_context: Optional[Dict] = None,
) -> Dict:
    """
    Build the complete data package for the AI Agent.
    
    Args:
        brain_result: BrainResult from central_brain.py
        tech_data: Technical analysis data
        ml_data: ML predictions
        momentum_data: Momentum detector data
        news_data: News and macro data
        calendar_data: Economic calendar data
        h1_candles: Last 20-30 H1 candles (OHLCV)
        m5_candles: Last 10 M5 candles (OHLCV)
        current_price: Current bid/ask/spread
        positions: Open positions list
        session_context: Session info and recent performance
        volatility_status: Volatility guard status
        sr_zones: List of SRZone objects (4-8 nearest zones)
        candlestick_patterns: Dict from detect_candlestick_patterns()
        sr_proximity: Dict with near_strong_zone and distance info
        d1_candles: Last 5-10 D1 candles (weekly context)
        h4_candles: Last 10-15 H4 candles (2-3 day structure)
        agent_memory: Last 3-5 Agent decisions for self-reference
        trade_feedback: Recent trade results with Agent accuracy
        delta_context: What changed since last cycle
        portfolio: Daily P&L, W/L, drawdown, risk budget
        regime_context: Trending/ranging, ADX/ATR analysis
        
    Returns:
        Complete data package dict for Agent
    """
    try:
        session_memory = load_session_memory()

        # Get current price value for S/R zone formatting
        price_val = 0
        if current_price:
            price_val = current_price.get("bid", current_price.get("ask", 0))

        formatted_sr_zones = _format_sr_zones(sr_zones or [], price_val)
        nearest_support = _compute_nearest_sr(formatted_sr_zones, side="below")
        nearest_resistance = _compute_nearest_sr(formatted_sr_zones, side="above")
        
        package = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "current_price": _format_current_price(current_price),
            "h1_candles": _format_candles(h1_candles, limit=50),
            "m5_candles": _format_candles(m5_candles, limit=10),
            "d1_candles": _format_candles(d1_candles or [], limit=10),
            "h4_candles": _format_candles(h4_candles or [], limit=20),
            "indicators": _format_indicators(tech_data, momentum_data),
            "brain_analysis": _format_brain_result(brain_result),
            "ml_predictions": _format_ml_data(ml_data),
            "macro": _format_macro_data(news_data, calendar_data),
            "positions": _format_positions(positions),
            "session": _format_session_context(session_context),
            "volatility": _format_volatility(volatility_status),
            "sr_zones": formatted_sr_zones,
            "nearest_support": nearest_support,
            "nearest_resistance": nearest_resistance,
            "candlestick_patterns": _format_candlestick_patterns(candlestick_patterns),
            "sr_proximity": _format_sr_proximity(sr_proximity),
            "agent_memory": _format_agent_memory(agent_memory or []),
            "trade_feedback": _format_trade_feedback(trade_feedback),
            "delta_context": _format_delta_context(delta_context),
            "portfolio": _format_portfolio(portfolio),
            "regime_context": _format_regime_context(regime_context),
            "session_memory": session_memory,
        }
        
        return package
        
    except Exception as e:
        logger.error(f"Error building data package: {e}")
        return _minimal_package(brain_result, current_price)


def build_proactive_data_package(
    brain_result: Any,
    tech_data: Dict,
    ml_data: Dict,
    momentum_data: Dict,
    news_data: Dict,
    calendar_data: Dict,
    h1_candles: List[Dict],
    m5_candles: List[Dict],
    current_price: Dict,
    positions: List[Dict],
    session_context: Dict,
    volatility_status: Dict,
    sr_zones: Optional[List] = None,
    candlestick_patterns: Optional[Dict] = None,
    sr_proximity: Optional[Dict] = None,
    d1_candles: Optional[List[Dict]] = None,
    h4_candles: Optional[List[Dict]] = None,
    trade_feedback: Optional[Dict] = None,
    ema200: Optional[float] = None,
    recent_decisions: Optional[List[Dict]] = None,
    trade_history: Optional[List[Dict]] = None,
    last_execution_result: Optional[Dict] = None,
) -> Dict:
    """Build an independent data package for proactive Agent snapshots.

    Excludes Brain opinion/scoring and agent_memory_context; includes only raw market context.
    """
    try:
        session_memory = load_session_memory()

        price_val = 0
        if current_price:
            price_val = current_price.get("bid", current_price.get("ask", 0))

        formatted_sr_zones = _format_sr_zones(sr_zones or [], price_val)
        nearest_support = _compute_nearest_sr(formatted_sr_zones, side="below")
        nearest_resistance = _compute_nearest_sr(formatted_sr_zones, side="above")

        mtf_trend = None
        try:
            if brain_result is not None and hasattr(brain_result, "mtf_trend"):
                mtf = getattr(brain_result, "mtf_trend", None) or {}
                mtf_trend = {
                    "d1_direction": mtf.get("d1_direction"),
                    "h4_direction": mtf.get("h4_direction"),
                }
        except Exception:
            mtf_trend = None

        package = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "current_price": _format_current_price(current_price),
            "h1_candles": _format_candles(h1_candles, limit=50),
            "m5_candles": _format_candles(m5_candles, limit=10),
            "d1_candles": _format_candles(d1_candles or [], limit=10),
            "h4_candles": _format_candles(h4_candles or [], limit=20),
            "indicators": _format_indicators(tech_data, momentum_data),
            "ml_predictions": _format_ml_data(ml_data),
            "macro": _format_macro_data(news_data, calendar_data),
            "positions": _format_positions(positions),
            "session": _format_session_context(session_context),
            "volatility": _format_volatility(volatility_status),
            "sr_zones": formatted_sr_zones,
            "nearest_support": nearest_support,
            "nearest_resistance": nearest_resistance,
            "candlestick_patterns": _format_candlestick_patterns(candlestick_patterns),
            "sr_proximity": _format_sr_proximity(sr_proximity),
            "trade_feedback": _format_trade_feedback(trade_feedback),
            "mtf_trend": mtf_trend,
            "recent_decisions": recent_decisions or [],
            "trade_history": trade_history or [],
            "last_execution_result": last_execution_result or {},
            "session_memory": session_memory,
        }

        try:
            active = None
            try:
                from db_writer import get_active_trade_from_proactive
                active = get_active_trade_from_proactive()
            except Exception:
                active = None

            if active and isinstance(active, dict) and active.get("decision") in ("OPEN_BUY", "OPEN_SELL") and active.get("entry") is not None:
                direction = "BUY" if active.get("decision") == "OPEN_BUY" else "SELL"
                entry_price = float(active.get("entry"))
                sl = active.get("sl")
                tp = active.get("tp")

                bid = None
                ask = None
                try:
                    bid = float((current_price or {}).get("bid"))
                except Exception:
                    bid = None
                try:
                    ask = float((current_price or {}).get("ask"))
                except Exception:
                    ask = None

                current_used = bid if direction == "SELL" else ask
                if current_used is None:
                    current_used = bid if bid is not None else ask

                if current_used is not None:
                    pnl_points = (entry_price - current_used) if direction == "SELL" else (current_used - entry_price)

                    dist_sl = None
                    dist_tp = None
                    try:
                        if sl is not None:
                            sl_f = float(sl)
                            dist_sl = abs(sl_f - current_used)
                    except Exception:
                        dist_sl = None
                    try:
                        if tp is not None:
                            tp_f = float(tp)
                            dist_tp = abs(current_used - tp_f)
                    except Exception:
                        dist_tp = None

                    pnl_status = "BREAKEVEN"
                    if pnl_points > 0:
                        pnl_status = "WINNING"
                    elif pnl_points < 0:
                        pnl_status = "LOSING"

                    package["active_trade_context"] = {
                        "entry": {
                            "direction": direction,
                            "price": _safe_round(entry_price, 2),
                            "timestamp": active.get("timestamp", ""),
                        },
                        "current_price": _safe_round(current_used, 2),
                        "pnl_points": _safe_round(pnl_points, 2),
                        "pnl_status": pnl_status,
                        "distance_to_sl": _safe_round(dist_sl, 2) if dist_sl is not None else None,
                        "distance_to_tp": _safe_round(dist_tp, 2) if dist_tp is not None else None,
                        "sl": _safe_round(float(sl), 2) if sl is not None else None,
                        "tp": _safe_round(float(tp), 2) if tp is not None else None,
                    }

                    # Enrich with EA phase + current SL when available
                    try:
                        from ea_bridge import read_ea_status

                        status = read_ea_status(stale_threshold_seconds=120)
                        if status and getattr(status, "positions", None):
                            phase = None
                            current_sl = None
                            # Prefer matching direction; otherwise use first position
                            for pos in status.positions:
                                try:
                                    if str(getattr(pos, "direction", "")).upper() == direction:
                                        phase = getattr(pos, "phase", None)
                                        current_sl = getattr(pos, "sl", None)
                                        break
                                except Exception:
                                    continue
                            if phase is None or current_sl is None:
                                try:
                                    pos0 = status.positions[0]
                                    phase = phase or getattr(pos0, "phase", None)
                                    current_sl = current_sl or getattr(pos0, "sl", None)
                                except Exception:
                                    pass

                            if phase is not None:
                                package["active_trade_context"]["phase"] = phase
                            if current_sl is not None:
                                package["active_trade_context"]["current_sl"] = _safe_round(float(current_sl), 2)
                    except Exception:
                        pass
        except Exception:
            pass

        if ema200 is not None:
            try:
                package["indicators"].setdefault("emas", {})
                package["indicators"]["emas"]["ema200"] = _safe_round(ema200, 2)
            except Exception:
                pass

        return package
    except Exception as e:
        logger.error(f"Error building proactive data package: {e}")
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "current_price": _format_current_price(current_price),
            "error": "Partial proactive package - some components failed to load",
        }


def _format_current_price(price_data: Dict) -> Dict:
    """Format current price data"""
    if not price_data:
        return {"bid": 0, "ask": 0, "spread": 0}
    
    return {
        "bid": _safe_round(price_data.get("bid", 0), 2),
        "ask": _safe_round(price_data.get("ask", 0), 2),
        "spread": _safe_round(price_data.get("spread", 0), 1),
    }


def _format_candles(candles: List[Dict], limit: int = 20) -> List[Dict]:
    """
    Format candle data for the Agent.
    Keep only essential OHLCV data, limit to recent candles.
    """
    if not candles:
        return []
    
    # Take most recent candles
    recent = candles[-limit:] if len(candles) > limit else candles
    
    formatted = []
    for c in recent:
        formatted.append({
            "time": c.get("time", ""),
            "o": _safe_round(c.get("open", 0), 2),
            "h": _safe_round(c.get("high", 0), 2),
            "l": _safe_round(c.get("low", 0), 2),
            "c": _safe_round(c.get("close", 0), 2),
            "v": int(c.get("tick_volume", c.get("volume", 0)) or 0),
        })
    
    return formatted


def _format_indicators(tech_data: Dict, momentum_data: Dict) -> Dict:
    """Format technical indicators for the Agent"""
    indicators = {}
    
    # RSI
    rsi = tech_data.get("rsi", {})
    indicators["rsi"] = {
        "value": _safe_round(rsi.get("value", 50), 1),
        "level": rsi.get("level", "neutral"),
    }
    
    # MACD
    macd = tech_data.get("macd", {})
    indicators["macd"] = {
        "histogram": _safe_round(macd.get("histogram", 0), 3),
        "signal": macd.get("signal", "neutral"),
        "trend": macd.get("trend", "neutral"),
    }
    
    # EMAs
    ema = tech_data.get("ema", {})
    indicators["emas"] = {
        "ema9": _safe_round(ema.get("ema9", 0), 2),
        "ema21": _safe_round(ema.get("ema21", 0), 2),
        "ema50": _safe_round(ema.get("ema50", 0), 2),
        "ema200": _safe_round(ema.get("ema200", 0), 2),
        "above_ema20": ema.get("above_ema20", False),
        "above_ema50": ema.get("above_ema50", False),
        "above_ema200": ema.get("above_ema200", False),
    }
    
    # Bollinger Bands
    bb = tech_data.get("bollinger", {})
    indicators["bollinger"] = {
        "upper": _safe_round(bb.get("upper", 0), 2),
        "middle": _safe_round(bb.get("middle", 0), 2),
        "lower": _safe_round(bb.get("lower", 0), 2),
        "position": _safe_round(bb.get("position", 0.5), 2),  # 0-1 where price is in band
        "squeeze": bb.get("squeeze", False),
    }
    
    # ATR
    atr = momentum_data.get("atr", {})
    indicators["atr"] = {
        "value": _safe_round(atr.get("atr_value", 0), 2),
        "trend": atr.get("atr_trend", "stable"),
    }
    
    # ADX
    adx = momentum_data.get("adx", {})
    indicators["adx"] = {
        "value": _safe_round(adx.get("adx_value", 0), 1),
        "plus_di": _safe_round(adx.get("plus_di", 0), 1),
        "minus_di": _safe_round(adx.get("minus_di", 0), 1),
        "classification": adx.get("adx_classification", "weak"),
    }
    
    # Volume (tick volume - XAU/USD has no real volume data)
    volume = momentum_data.get("volume", {})
    indicators["volume"] = {
        "tick_volume_ratio": _safe_round(volume.get("volume_ratio", 1.0), 2),
        "classification": volume.get("volume_classification", "normal"),
    }
    
    return indicators


def _format_brain_result(brain_result: Any) -> Dict:
    """Format Brain analysis for the Agent"""
    if brain_result is None:
        return {
            "decision": "HOLD",
            "score": 50,
            "confidence": 50,
            "scenario": "unknown",
            "pillar_scores": {},
            "confirmations": [],
            "alerts": [],
            "mtf_trend": {"d1_direction": None, "h4_direction": None, "alignment": "n/a"},
            "volume_gate": {"volume_ratio": 1.0, "status": "normal"},
        }
    
    # Handle both BrainResult object and dict
    if hasattr(brain_result, "decision"):
        # Extract MTF trend data
        mtf_trend = getattr(brain_result, "mtf_trend", None) or {}
        mtf_trend_formatted = {
            "d1_direction": mtf_trend.get("d1_direction"),
            "h4_direction": mtf_trend.get("h4_direction"),
            "alignment": mtf_trend.get("alignment", "n/a"),
        }
        logger.debug(f"[Agent Data] MTF trend: d1={mtf_trend_formatted['d1_direction']}, h4={mtf_trend_formatted['h4_direction']}, alignment={mtf_trend_formatted['alignment']}")
        
        # Extract Volume Gate data
        volume_gate = getattr(brain_result, "volume_gate", None) or {}
        volume_gate_formatted = {
            "volume_ratio": _safe_round(volume_gate.get("volume_ratio", 1.0), 2),
            "status": volume_gate.get("status", "normal"),
        }
        
        return {
            "decision": brain_result.decision,
            "score": _safe_round(brain_result.final_score, 1),
            "confidence": _safe_round(brain_result.confidence, 1),
            "confidence_level": brain_result.confidence_level,
            "scenario": brain_result.scenario,
            "scenario_description": brain_result.scenario_description,
            "pillar_scores": {
                "technical": _safe_round(brain_result.adjusted_scores.get("technical", 50), 1),
                "ml": _safe_round(brain_result.adjusted_scores.get("ml", 50), 1),
                "momentum": _safe_round(brain_result.adjusted_scores.get("momentum", 50), 1),
                "news": _safe_round(brain_result.adjusted_scores.get("news", 50), 1),
                "calendar": _safe_round(brain_result.adjusted_scores.get("calendar", 50), 1),
            },
            "weights_used": brain_result.adjusted_weights,
            "confirmations": brain_result.confirmations[:5],  # Limit to 5
            "alerts": brain_result.alerts[:5],  # Limit to 5
            "mtf_trend": mtf_trend_formatted,
            "volume_gate": volume_gate_formatted,
        }
    else:
        # Dict format
        mtf_trend = brain_result.get("mtf_trend", {}) or {}
        volume_gate = brain_result.get("volume_gate", {}) or {}
        return {
            "decision": brain_result.get("decision", "HOLD"),
            "score": _safe_round(brain_result.get("final_score", 50), 1),
            "confidence": _safe_round(brain_result.get("confidence", 50), 1),
            "scenario": brain_result.get("scenario", "unknown"),
            "pillar_scores": brain_result.get("adjusted_scores", {}),
            "confirmations": brain_result.get("confirmations", [])[:5],
            "alerts": brain_result.get("alerts", [])[:5],
            "mtf_trend": {
                "d1_direction": mtf_trend.get("d1_direction"),
                "h4_direction": mtf_trend.get("h4_direction"),
                "alignment": mtf_trend.get("alignment", "n/a"),
            },
            "volume_gate": {
                "volume_ratio": _safe_round(volume_gate.get("volume_ratio", 1.0), 2),
                "status": volume_gate.get("status", "normal"),
            },
        }


def _format_ml_data(ml_data: Dict) -> Dict:
    """Format ML predictions for the Agent"""
    if not ml_data:
        return {
            "prediction": "neutral",
            "confidence": 0.5,
            "h1": {"bullish_prob": 0.5},
            "h4": {"bullish_prob": 0.5},
        }
    
    return {
        "prediction": ml_data.get("prediction", "neutral"),
        "confidence": _safe_round(ml_data.get("max_confidence", 0.5), 2),
        "pattern": ml_data.get("pattern", "undefined"),
        "h1": {
            "bullish_prob": _safe_round(ml_data.get("h1_bullish_prob", 0.5), 2),
            "confidence": _safe_round(ml_data.get("h1_confidence", 0.5), 2),
        },
        "h4": {
            "bullish_prob": _safe_round(ml_data.get("h4_bullish_prob", 0.5), 2),
            "confidence": _safe_round(ml_data.get("h4_confidence", 0.5), 2),
        },
        "ensemble_agreement": ml_data.get("ensemble_agreement", 0),
    }


def _format_macro_data(news_data: Dict, calendar_data: Dict) -> Dict:
    """Format news and macro data for the Agent"""
    macro = {}
    
    # Headlines (limit to 5 most recent)
    headlines = news_data.get("headlines", [])
    if isinstance(headlines, list):
        macro["headlines"] = headlines[:5]
    else:
        macro["headlines"] = []
    
    # DXY
    dxy = news_data.get("dxy", {})
    macro["dxy"] = {
        "value": _safe_round(dxy.get("value", 0), 2),
        "change_24h": _safe_round(dxy.get("change_24h", 0), 2),
        "trend": dxy.get("trend", "stable"),
    }
    
    # VIX
    vix = news_data.get("vix", {})
    macro["vix"] = {
        "value": _safe_round(vix.get("value", 0), 1),
        "level": vix.get("level", "normal"),
    }
    
    # Yields
    yields = news_data.get("yields", {})
    macro["yields_10y"] = {
        "value": _safe_round(yields.get("value", 0), 2),
        "trend": yields.get("trend", "stable"),
    }
    
    # Calendar
    cal_phase = calendar_data.get("phase", "normal")
    cal_bias = calendar_data.get("bias", "NEUTRAL")
    cal_source = calendar_data.get("source") or "mt5_bridge"

    # Build upcoming events list for the proactive Agent XML.
    # name/time/importance/time_until come from economic_calendar.get_upcoming_events().
    # forecast/previous come from economic_calendar.get_calendar_data()["events"] matched by event name.
    upcoming_enriched: List[Dict[str, Any]] = []
    try:
        from economic_calendar import get_upcoming_events

        upcoming_basic = get_upcoming_events(max_events=5)
        if not isinstance(upcoming_basic, list):
            upcoming_basic = []

        raw_events = calendar_data.get("events", [])

        if not isinstance(raw_events, list):
            raw_events = []

        lookup: Dict[str, Dict[str, Any]] = {}
        for e in raw_events:
            if not isinstance(e, dict):
                continue
            k = _normalize_event_name(e.get("name"))
            if not k:
                continue
            lookup[k] = e

        for ev in upcoming_basic[:5]:
            if not isinstance(ev, dict):
                continue
            name = ev.get("name")
            matched = lookup.get(_normalize_event_name(name)) or {}

            item: Dict[str, Any] = {
                "name": name,
                "time": ev.get("time"),
                "importance": ev.get("importance"),
                "time_until": ev.get("time_until"),
            }

            # Attach optional values if present
            fv = matched.get("forecast_value")
            pv = matched.get("previous_value")
            if fv is not None:
                item["forecast"] = fv
            if pv is not None:
                item["previous"] = pv

            upcoming_enriched.append(item)
    except Exception:
        upcoming_enriched = []

    macro["calendar"] = {
        "phase": cal_phase,
        "bias": cal_bias,
        "source": cal_source,
        "score": _safe_round(calendar_data.get("score", 50), 1),
        "next_event": calendar_data.get("next_event_name", ""),
        "next_event_in": calendar_data.get("next_event_minutes", 0),
        "upcoming_events": upcoming_enriched,
    }
    
    # Sentiment
    sentiment = news_data.get("sentiment", {})
    macro["sentiment"] = {
        "normalized": _safe_round(sentiment.get("normalized", 0), 2),
        "label": sentiment.get("label", "neutral"),
    }
    
    return macro


def _format_positions(positions: List[Dict]) -> List[Dict]:
    """Format open positions for the Agent"""
    if not positions:
        return []
    
    formatted = []
    for pos in positions[:3]:  # Max 3 positions
        formatted.append({
            "ticket": pos.get("ticket", 0),
            "direction": pos.get("type", "unknown"),
            "entry_price": _safe_round(pos.get("price_open", 0), 2),
            "current_price": _safe_round(pos.get("price_current", 0), 2),
            "profit_pips": _safe_round(pos.get("profit_pips", 0), 1),
            "profit_usd": _safe_round(pos.get("profit", 0), 2),
            "sl": _safe_round(pos.get("sl", 0), 2),
            "tp": _safe_round(pos.get("tp", 0), 2),
            "duration_hours": _safe_round(pos.get("duration_hours", 0), 1),
            "phase": pos.get("phase", "active"),  # active, breakeven, trailing
        })
    
    return formatted


def _format_session_context(session_context: Dict) -> Dict:
    """Format session and recent performance context"""
    if not session_context:
        return {
            "name": "unknown",
            "today_trades": 0,
            "today_wl": "0W/0L",
            "today_pnl": 0,
            "last_5_results": [],
        }
    
    return {
        "name": session_context.get("session_name", "unknown"),
        "hour_utc": session_context.get("hour_utc", 0),
        "today_trades": session_context.get("today_trades", 0),
        "today_wins": session_context.get("today_wins", 0),
        "today_losses": session_context.get("today_losses", 0),
        "today_pnl": _safe_round(session_context.get("today_pnl", 0), 2),
        "last_5_results": session_context.get("last_5_results", []),
        "consecutive_losses": session_context.get("consecutive_losses", 0),
    }


def _format_volatility(volatility_status: Dict) -> Dict:
    """Format volatility guard status"""
    if not volatility_status:
        return {
            "status": "NORMAL",
            "m5_move_pct": 0,
        }
    
    return {
        "status": volatility_status.get("status", "NORMAL"),
        "m5_move_pct": _safe_round(volatility_status.get("extreme_percent", 0), 2),
        "cooling_until": volatility_status.get("cooling_until", ""),
    }


def _format_sr_zones(sr_zones: List, current_price: float, max_zones: int = 8) -> List[Dict]:
    """
    Format S/R zones for the Agent.
    Returns 4 zones above and 4 zones below current price (nearest first).
    
    Args:
        sr_zones: List of SRZone objects from support_resistance.py
        current_price: Current price for distance calculation
        max_zones: Maximum total zones to return (default 8)
    
    Returns:
        List of formatted zone dicts
    """
    if not sr_zones or not current_price:
        return []
    
    PIP_SIZE = 0.01
    
    # Split into above and below current price
    above = []
    below = []
    
    for zone in sr_zones:
        # Handle both SRZone objects and dicts
        if hasattr(zone, "midpoint"):
            midpoint = zone.midpoint
            zone_type = zone.zone_type
            touches = zone.touches
            timeframe = zone.timeframe
            strength = zone.strength
            confluence = getattr(zone, "confluence", [])
        else:
            midpoint = zone.get("midpoint", zone.get("price", 0))
            zone_type = zone.get("zone_type", "UNKNOWN")
            touches = zone.get("touches", 0)
            timeframe = zone.get("timeframe", "H1")
            strength = zone.get("strength", "weak")
            confluence = zone.get("confluence", [])
        
        dist_pips = abs(midpoint - current_price) / PIP_SIZE
        
        formatted = {
            "price": _safe_round(midpoint, 2),
            "zone_type": zone_type,
            "touches": touches,
            "timeframe": timeframe,
            "strength": strength,
            "dist_pips": _safe_round(dist_pips, 0),
            "position": "above" if midpoint > current_price else "below",
            "confluence": confluence if confluence else [],
        }
        
        if midpoint > current_price:
            above.append(formatted)
        else:
            below.append(formatted)
    
    # Sort: above by distance ascending (nearest first), below by distance ascending
    above.sort(key=lambda z: z["dist_pips"])
    below.sort(key=lambda z: z["dist_pips"])
    
    # Take 4 nearest from each side
    half = max_zones // 2
    result = above[:half] + below[:half]
    
    return result


def _compute_nearest_sr(formatted_sr_zones: List[Dict], side: str) -> Optional[Dict]:
    """Compute nearest support/resistance from formatted zones."""
    if not formatted_sr_zones:
        return None

    nearest = None
    for z in formatted_sr_zones:
        if z.get("position") != side:
            continue
        if z.get("price") is None or z.get("dist_pips") is None:
            continue
        if nearest is None or float(z.get("dist_pips", 1e9)) < float(nearest.get("distance_pips", 1e9)):
            nearest = {
                "level": z.get("price"),
                "distance_pips": z.get("dist_pips"),
            }

    return nearest


def _format_candlestick_patterns(patterns_data: Dict) -> Dict:
    """
    Format candlestick patterns for the Agent.
    
    Args:
        patterns_data: Dict from detect_candlestick_patterns()
    
    Returns:
        Formatted dict with primary pattern and all detected patterns
    """
    if not patterns_data:
        return {
            "primary_pattern": None,
            "patterns": [],
            "sr_multiplier": 1.0,
            "sr_context": None,
        }
    
    primary = patterns_data.get("primary_pattern")
    primary_formatted = None
    if primary:
        primary_formatted = {
            "name": primary.get("name", ""),
            "direction": primary.get("direction", ""),
            "base_score": primary.get("base_score", 0),
            "sr_multiplier": primary.get("sr_multiplier", 1.0),
            "final_score": primary.get("final_score", 0),
        }
    
    # Format all patterns (limit to 3)
    all_patterns = []
    for p in patterns_data.get("patterns", [])[:3]:
        all_patterns.append({
            "name": p.get("name", ""),
            "direction": p.get("direction", ""),
            "score": p.get("final_score", 0),
        })
    
    return {
        "primary_pattern": primary_formatted,
        "patterns": all_patterns,
        "sr_multiplier": patterns_data.get("sr_multiplier", 1.0),
        "sr_context": patterns_data.get("sr_context"),
    }


def _format_sr_proximity(sr_proximity_data: Dict) -> Dict:
    """
    Format S/R proximity data for the Agent.
    
    Args:
        sr_proximity_data: Dict with near_strong_zone and distance info
    
    Returns:
        Formatted dict
    """
    if not sr_proximity_data:
        return {
            "near_strong_zone": False,
            "nearest_zone_dist_pips": None,
            "nearest_zone_info": None,
        }
    
    zone_info = sr_proximity_data.get("near_zone_info")
    zone_info_formatted = None
    if zone_info:
        zone_info_formatted = {
            "price": zone_info.get("price"),
            "zone_type": zone_info.get("zone_type"),
            "touches": zone_info.get("touches"),
            "timeframe": zone_info.get("timeframe"),
        }
    
    return {
        "near_strong_zone": sr_proximity_data.get("near_strong_zone", False),
        "nearest_zone_dist_pips": sr_proximity_data.get("dist_to_nearest_pips"),
        "nearest_zone_info": zone_info_formatted,
    }


def _format_agent_memory(recent_decisions: List[Dict]) -> Dict:
    """
    Format Agent memory (recent decisions) for self-reference.
    Converts timestamps to relative time.
    """
    if not recent_decisions:
        return {"recent_decisions": []}
    
    now = datetime.now(timezone.utc)
    formatted = []
    
    for decision in recent_decisions[:5]:  # Max 5
        timestamp_str = decision.get("timestamp", "")
        relative_time = "unknown"
        
        # Convert timestamp to relative time
        if timestamp_str:
            try:
                # Parse ISO timestamp
                if "T" in timestamp_str:
                    dt = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
                else:
                    dt = datetime.fromisoformat(timestamp_str)
                
                # Make timezone-aware if needed
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                
                delta = now - dt
                minutes = int(delta.total_seconds() / 60)
                
                if minutes < 1:
                    relative_time = "just now"
                elif minutes < 60:
                    relative_time = f"{minutes} min ago"
                elif minutes < 1440:
                    hours = minutes // 60
                    relative_time = f"{hours} hr ago"
                else:
                    days = minutes // 1440
                    relative_time = f"{days} day ago"
            except Exception:
                relative_time = "unknown"
        
        formatted.append({
            "time": relative_time,
            "trigger": decision.get("trigger", "SIGNAL"),
            "decision": decision.get("decision", "UNKNOWN"),
            "reasoning_summary": decision.get("reasoning_summary", ""),
        })
    
    return {"recent_decisions": formatted}


def _format_trade_feedback(feedback_data: Optional[Dict]) -> Dict:
    """Format trade feedback with Agent accuracy stats."""
    if not feedback_data:
        return {
            "last_trades": [],
            "agent_accuracy": {
                "total_decisions": 0,
                "correct_rejects": 0,
                "incorrect_rejects": 0,
                "correct_opens": 0,
                "incorrect_opens": 0,
            }
        }
    
    return {
        "last_trades": feedback_data.get("last_trades", [])[:5],
        "agent_accuracy": feedback_data.get("agent_accuracy", {}),
    }


def _format_delta_context(delta_data: Optional[Dict]) -> Dict:
    """Format delta context (what changed since last cycle)."""
    if not delta_data:
        return {
            "price_change_pips": 0,
            "rsi_change": 0,
            "volume_change_pct": 0,
            "significant_events": [],
        }
    
    return {
        "price_change_pips": _safe_round(delta_data.get("price_change_pips", 0), 1),
        "rsi_change": _safe_round(delta_data.get("rsi_change", 0), 1),
        "volume_change_pct": _safe_round(delta_data.get("volume_change_pct", 0), 1),
        "significant_events": delta_data.get("significant_events", [])[:5],
    }


def _format_portfolio(portfolio_data: Optional[Dict]) -> Dict:
    """Format portfolio awareness data."""
    if not portfolio_data:
        return {
            "daily_pnl": 0,
            "daily_wins": 0,
            "daily_losses": 0,
            "win_rate_today": 0,
            "drawdown_pct": 0,
            "risk_budget_remaining_pct": 100,
        }
    
    return {
        "daily_pnl": _safe_round(portfolio_data.get("daily_pnl", 0), 2),
        "daily_wins": portfolio_data.get("daily_wins", 0),
        "daily_losses": portfolio_data.get("daily_losses", 0),
        "win_rate_today": _safe_round(portfolio_data.get("win_rate_today", 0), 1),
        "drawdown_pct": _safe_round(portfolio_data.get("drawdown_pct", 0), 2),
        "risk_budget_remaining_pct": _safe_round(portfolio_data.get("risk_budget_remaining_pct", 100), 1),
    }


def _format_regime_context(regime_data: Optional[Dict]) -> Dict:
    """Format regime context (trending/ranging, ADX/ATR analysis)."""
    if not regime_data:
        return {
            "regime": "unknown",
            "adx_hours_above_25": 0,
            "atr_vs_weekly_avg": 1.0,
            "trend_strength": "unknown",
        }
    
    return {
        "regime": regime_data.get("regime", "unknown"),
        "adx_hours_above_25": regime_data.get("adx_hours_above_25", 0),
        "atr_vs_weekly_avg": _safe_round(regime_data.get("atr_vs_weekly_avg", 1.0), 2),
        "trend_strength": regime_data.get("trend_strength", "unknown"),
    }


def _minimal_package(brain_result: Any, current_price: Dict) -> Dict:
    """Create minimal package when full build fails"""
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "current_price": _format_current_price(current_price),
        "brain_analysis": _format_brain_result(brain_result),
        "error": "Partial data package - some components failed to load",
    }


def get_session_name(hour_utc: int) -> str:
    """
    Get trading session name from UTC hour.
    
    Sessions:
    - Asian: 00:00-08:00 UTC
    - London: 08:00-16:00 UTC
    - New York: 13:00-21:00 UTC (overlaps with London 13:00-16:00)
    """
    if 0 <= hour_utc < 8:
        return "Asian"
    elif 8 <= hour_utc < 13:
        return "London"
    elif 13 <= hour_utc < 16:
        return "London/NY"
    elif 16 <= hour_utc < 21:
        return "New York"
    else:
        return "After Hours"


# =============================================================================
# TESTS
# =============================================================================

def _test_data_builder():
    """Test the data builder with mock data"""
    print("=" * 60)
    print("📦 DATA BUILDER TEST")
    print("=" * 60)
    
    # Mock Brain result
    class MockBrainResult:
        decision = "BUY"
        final_score = 68.2
        confidence = 72.0
        confidence_level = "HIGH"
        scenario = "momentum_forte_confirmado"
        scenario_description = "Strong momentum confirmed"
        adjusted_scores = {"technical": 65, "ml": 70, "momentum": 75, "news": 55, "calendar": 50}
        adjusted_weights = {"technical": 0.30, "ml": 0.25, "momentum": 0.15, "news": 0.20, "calendar": 0.10}
        confirmations = ["ADX strong: 32", "ML bullish confirmed"]
        alerts = ["DXY rising +0.3%"]
    
    # Mock data
    mock_tech = {
        "rsi": {"value": 62, "level": "neutral"},
        "macd": {"histogram": 0.5, "signal": "bullish"},
        "ema": {"ema9": 2912, "ema21": 2908, "ema50": 2900},
        "bollinger": {"upper": 2930, "middle": 2915, "lower": 2900, "position": 0.5},
    }
    
    mock_momentum = {
        "adx": {"adx_value": 32, "plus_di": 28, "minus_di": 18},
        "atr": {"atr_value": 28.5, "atr_trend": "stable"},
        "volume": {"volume_ratio": 1.2, "volume_classification": "high"},
    }
    
    mock_news = {
        "headlines": ["Fed signals patience", "Gold steady"],
        "dxy": {"value": 103.8, "change_24h": 0.3, "trend": "rising"},
        "vix": {"value": 18.2, "level": "normal"},
        "yields": {"value": 4.25, "trend": "stable"},
        "sentiment": {"normalized": 0.1, "label": "neutral"},
    }
    
    mock_calendar = {
        "phase": "normal",
        "bias": "NEUTRAL",
        "score": 50,
    }
    
    mock_candles = [
        {"time": "2026-03-05T10:00:00", "open": 2910, "high": 2918, "low": 2908, "close": 2915, "tick_volume": 1234},
    ]
    
    mock_price = {"bid": 2915.50, "ask": 2915.80, "spread": 3.0}
    
    mock_positions = []
    
    mock_session = {
        "session_name": "London",
        "hour_utc": 10,
        "today_trades": 2,
        "today_wins": 1,
        "today_losses": 1,
        "today_pnl": 12.50,
    }
    
    mock_volatility = {"status": "NORMAL"}
    
    # Build package
    package = build_data_package(
        brain_result=MockBrainResult(),
        tech_data=mock_tech,
        ml_data={},
        momentum_data=mock_momentum,
        news_data=mock_news,
        calendar_data=mock_calendar,
        h1_candles=mock_candles,
        m5_candles=mock_candles,
        current_price=mock_price,
        positions=mock_positions,
        session_context=mock_session,
        volatility_status=mock_volatility,
    )
    
    import json
    print("\nGenerated package:")
    print(json.dumps(package, indent=2, default=str))
    
    # Estimate tokens
    json_str = json.dumps(package)
    est_tokens = len(json_str) // 4
    print(f"\nEstimated tokens: ~{est_tokens}")


if __name__ == "__main__":
    _test_data_builder()
