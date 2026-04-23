"""
FLO-332: Session-specific market context.

Answers Floki's most-repeated question: "How does the current session
compare to normal for this session?"

For the session Floki is currently in (ASIAN / LONDON / NY), compares
current volume and range against the same metrics from the last N same
sessions, normalized to "typical at the same elapsed minutes into the
session" so comparisons are fair when the current session is only
partially unfolded.

Session boundaries (UTC):
  ASIAN  : 22:00 - 08:00   (includes 22:00-24:00 pre-Asian transition)
  LONDON : 08:00 - 14:00
  NY     : 14:00 - 22:00

Historical: M15 candles via MT5. Default window = 20 same sessions.

Never raises - returns None on any failure.
"""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import mean, pstdev
from typing import Any, Dict, List, Optional, Tuple

from mt5_safe import mt5  # FLO-348: thread-safe MT5 proxy

from logger import log


_MAX_WINDOW_SESSIONS = 40
_MIN_TRADES_FOR_WIN_RATE = 10
_M15_MIN = 15
_BARS_PER_DAY = 96  # M15: 24h * 4
_FETCH_COUNT = _BARS_PER_DAY * 30  # 30 days buffer for 20-session window + weekends


def _broker_offset_hours(symbol: str = "XAUUSD") -> int:
    """Return the broker's UTC offset in hours (MT5 bar epochs are broker-time, FLO-96).

    Derived dynamically from symbol_info_tick vs real UTC. Falls back to +3 (EEST).
    """
    try:
        tick = mt5.symbol_info_tick(symbol)
        if tick is None or not tick.time:
            return 3
        broker_dt = datetime.fromtimestamp(int(tick.time), tz=timezone.utc).replace(tzinfo=None)
        real_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        return int(round((broker_dt - real_utc).total_seconds() / 3600))
    except Exception:
        return 3


def _bar_time_utc(bar: Any, offset_hours: int) -> datetime:
    """Convert an MT5 bar's `time` (broker epoch) to real UTC naive datetime."""
    broker_naive = datetime.fromtimestamp(int(bar["time"]), tz=timezone.utc).replace(tzinfo=None)
    return broker_naive - timedelta(hours=offset_hours)


def _session_of_hour(hour_utc: int) -> str:
    if 8 <= hour_utc < 14:
        return "LONDON"
    if 14 <= hour_utc < 22:
        return "NY"
    return "ASIAN"


def _current_session_window(ref_utc: datetime) -> Tuple[str, datetime, datetime]:
    """Given a UTC timestamp, return (session_name, session_start, session_end)."""
    day = ref_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    h = ref_utc.hour
    if 8 <= h < 14:
        return "LONDON", day + timedelta(hours=8), day + timedelta(hours=14)
    if 14 <= h < 22:
        return "NY", day + timedelta(hours=14), day + timedelta(hours=22)
    # ASIAN spans 22:00 prev day - 08:00 current day
    if h >= 22:
        return "ASIAN", day + timedelta(hours=22), day + timedelta(days=1, hours=8)
    return "ASIAN", day - timedelta(hours=2), day + timedelta(hours=8)


def _classify(z: Optional[float]) -> str:
    if z is None:
        return "normal"
    if z <= -2.0 or z >= 2.0:
        return "extreme"
    if z <= -1.0:
        return "below_normal"
    if z >= 1.0:
        return "above_normal"
    return "normal"


def _overall_classification(volume_cls: str, range_cls: str) -> str:
    order = {"below_normal": 0, "normal": 1, "above_normal": 2, "extreme": 3}
    return max(volume_cls, range_cls, key=lambda c: order.get(c, 1))


def _percentile(values: List[float], current: float) -> Optional[int]:
    if not values:
        return None
    below = sum(1 for v in values if v < current)
    return int(round(100.0 * below / len(values)))


def _z_score(values: List[float], current: float) -> Optional[float]:
    if len(values) < 2:
        return None
    m = mean(values)
    s = pstdev(values)
    if s == 0:
        return 0.0
    return round((current - m) / s, 2)


def _cumulative_at_elapsed(
    bars: List[Tuple[datetime, Any]],
    session_start: datetime,
    elapsed_min: int,
) -> Tuple[int, float]:
    """For a list of (bar_time_utc, bar) belonging to ONE session, compute cumulative
    (volume, range_pts) across bars that OPENED before (session_start + elapsed_min)."""
    cutoff = session_start + timedelta(minutes=elapsed_min)
    total_vol = 0
    hi = float("-inf")
    lo = float("inf")
    for bt, b in bars:
        if bt >= cutoff:
            continue
        total_vol += int(b["tick_volume"])
        bh = float(b["high"])
        bl = float(b["low"])
        if bh > hi:
            hi = bh
        if bl < lo:
            lo = bl
    if hi == float("-inf") or lo == float("inf"):
        return 0, 0.0
    return total_vol, round(hi - lo, 2)


def _win_rate_for_session(session: str) -> Dict[str, Any]:
    """Query trades table for win rate of this session across full history.
    Returns {'status': 'n_insufficient', 'n': N} if under _MIN_TRADES_FOR_WIN_RATE.
    """
    import os
    import sqlite3

    db = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "history.db")
    if not os.path.isfile(db):
        return {"status": "no_db"}
    try:
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT open_time, profit FROM trades "
            "WHERE close_time IS NOT NULL AND profit IS NOT NULL AND ticket != 0 "
            "ORDER BY open_time DESC LIMIT 1000"
        ).fetchall()
        conn.close()
    except Exception as e:
        log.debug(f"get_session_context | win_rate DB error: {e}")
        return {"status": "error"}

    wins = 0
    losses = 0
    for r in rows:
        ot = r["open_time"]
        if not ot or len(ot) < 13:
            continue
        try:
            hr = int(ot[11:13])
        except Exception:
            continue
        if _session_of_hour(hr) != session:
            continue
        p = r["profit"]
        if p is None:
            continue
        if float(p) > 0:
            wins += 1
        else:
            losses += 1
    n = wins + losses
    if n < _MIN_TRADES_FOR_WIN_RATE:
        return {"status": "n_insufficient", "n": n}
    return {
        "status": "ok",
        "wins": wins,
        "losses": losses,
        "n": n,
        "rate": round(wins / n, 2),
    }


def compute_session_context(
    symbol: str = "XAUUSD",
    window_sessions: int = 20,
    now_utc: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """Return session-context dict. None on any failure (no data / MT5 error).

    Args:
        symbol: MT5 symbol (default XAUUSD).
        window_sessions: number of historical same sessions to compare against.
        now_utc: override "now" for testing (must be naive UTC).
    """
    try:
        window = max(5, min(_MAX_WINDOW_SESSIONS, int(window_sessions)))

        if now_utc is None:
            now_utc = datetime.now(timezone.utc).replace(tzinfo=None)

        session, s_start, s_end = _current_session_window(now_utc)
        session_length_min = int((s_end - s_start).total_seconds() / 60)
        elapsed_min = int((now_utc - s_start).total_seconds() / 60)
        elapsed_min = max(0, min(elapsed_min, session_length_min))
        warmup = elapsed_min < _M15_MIN

        # FLO-96: MT5 bar epochs are broker-time. Anchor via copy_rates_from_pos
        # (avoids naive-datetime interpretation bugs on the window args) and convert
        # each bar's time back to real UTC with a dynamic broker offset.
        offset_h = _broker_offset_hours(symbol)
        fetch_count = max(_BARS_PER_DAY * (window + 10), _BARS_PER_DAY * 15)
        bars = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M15, 0, fetch_count)
        if bars is None or len(bars) == 0:
            log.debug("get_session_context | MT5 returned no M15 bars")
            return None

        by_session: Dict[str, List[Tuple[datetime, Any]]] = defaultdict(list)
        session_starts: Dict[str, datetime] = {}
        for b in bars:
            bt = _bar_time_utc(b, offset_h)
            sess_name, sess_start_b, _ = _current_session_window(bt)
            if sess_name != session:
                continue
            sid = f"{sess_name}_{sess_start_b.isoformat()}"
            by_session[sid].append((bt, b))
            session_starts[sid] = sess_start_b

        # Current session bars
        current_sid = f"{session}_{s_start.isoformat()}"
        current_bars = by_session.get(current_sid, [])
        # Historical sessions: everything else, most-recent first, capped to window
        historical_sids = sorted(
            (sid for sid in by_session if sid != current_sid),
            key=lambda sid: session_starts[sid],
            reverse=True,
        )[:window]

        cur_vol, cur_range = _cumulative_at_elapsed(current_bars, s_start, elapsed_min)

        # Historical cumulative at same elapsed_min
        hist_vols: List[float] = []
        hist_ranges: List[float] = []
        for sid in historical_sids:
            h_start = session_starts[sid]
            h_vol, h_range = _cumulative_at_elapsed(by_session[sid], h_start, elapsed_min)
            if h_vol > 0:
                hist_vols.append(float(h_vol))
            if h_range > 0:
                hist_ranges.append(float(h_range))

        def _metric_block(current: float, hist: List[float]) -> Dict[str, Any]:
            z = _z_score(hist, float(current))
            pct = _percentile(hist, float(current))
            return {
                "current": round(float(current), 2),
                "typical_at_this_elapsed": round(mean(hist), 2) if hist else None,
                "z_score": z,
                "percentile": pct,
                "classification": _classify(z),
            }

        volume_block = _metric_block(cur_vol, hist_vols)
        range_block = _metric_block(cur_range, hist_ranges)

        win_rate = _win_rate_for_session(session)

        return {
            "timestamp": now_utc.isoformat(timespec="seconds") + "Z",
            "session": session,
            "session_start_utc": s_start.isoformat(timespec="seconds") + "Z",
            "session_end_utc": s_end.isoformat(timespec="seconds") + "Z",
            "session_elapsed_min": elapsed_min,
            "session_length_min": session_length_min,
            "warmup": warmup,
            "n_historical_sessions": len(historical_sids),
            "volume": volume_block,
            "range_pts": range_block,
            "win_rate_session": win_rate,
            "overall_classification": _overall_classification(
                volume_block["classification"], range_block["classification"]
            ),
        }

    except Exception as e:
        log.debug(f"get_session_context | unexpected error: {e}")
        return None
