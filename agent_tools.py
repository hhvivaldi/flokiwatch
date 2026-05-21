import json
import os
import time
from datetime import datetime, timedelta, timezone
from tz_utils import utc_iso, utc_now  # FLO-286 / FLO-309
from typing import Any, Dict, Optional, List, Tuple

from logger import log


# FLO-422 Step 3 — passive author-time regime snapshot.
# When Floki successfully submits a plan with one of these setup_types,
# we compute a regime snapshot using breakout_regime.compute_regime_snapshot
# and persist to snow_plans.author_regime_snapshot_json. Fail-soft —
# any failure logs a warning and exits cleanly; the plan submission
# itself never fails because the snapshot couldn't be taken.
_FLO422_LIFECYCLE_SETUPS = (
    "breakout_range",
    "continuation_momentum",
    "pullback_trend",
    "structural_bounce",
)


def _maybe_persist_author_regime_snapshot(parsed_plan) -> None:
    """Compute + persist the author-time regime snapshot for lifecycle-
    sensitive plans. Fail-soft — never raises. Called immediately after
    a successful insert_plan(). No-op for non-qualifying setup_types.

    The snapshot captures the volatility-regime state Floki was authoring
    against. A separate trigger-time snapshot (Step 5) captures the same
    schema at the moment the entry conditions actually fire; comparing
    the two yields drift detection.
    """
    try:
        # Extract plan fields. The Plan is a Pydantic model from
        # snow.plan_schema; .analysis and .entry are nested models.
        plan_id = getattr(parsed_plan, "id", None)
        analysis = getattr(parsed_plan, "analysis", None)
        entry = getattr(parsed_plan, "entry", None)
        if plan_id is None or analysis is None or entry is None:
            return  # malformed — submission would have failed validation already

        setup_type = getattr(analysis, "setup_type", None)
        if setup_type not in _FLO422_LIFECYCLE_SETUPS:
            return  # not lifecycle-sensitive — nothing to snapshot

        direction = getattr(entry, "direction", None)
        entry_price = getattr(entry, "entry_price", None)
        if direction not in ("BUY", "SELL"):
            return

        # Snapshot timestamp = now (plan was just authored).
        snap_ts = datetime.now(timezone.utc)

        # ---- Fetch M5 candles via the proxy ----
        m5_candles = _flo422_fetch_m5_candles(snap_ts, n=30)

        # ---- Fetch analyses for the wider 24h window in a SINGLE query;
        #      filter the 4h slice in Python. One DB round-trip instead of two.
        analyses_24h = _flo422_fetch_analyses(snap_ts, minutes_back=24 * 60)
        cutoff_4h = (snap_ts.replace(tzinfo=None) if snap_ts.tzinfo else snap_ts) \
            - timedelta(minutes=240)
        cutoff_4h_iso = cutoff_4h.isoformat()[:19]
        analyses_4h = [a for a in analyses_24h
                       if a.get("timestamp") and a["timestamp"] >= cutoff_4h_iso]

        # ---- Determine current_price ----
        # Prefer the most recent M5 close; fall back to entry_price.
        current_price: float
        if m5_candles:
            current_price = float(m5_candles[-1].get("close") or entry_price or 0.0)
        elif entry_price is not None:
            current_price = float(entry_price)
        else:
            return  # genuinely no price reference; skip silently

        # ---- Compute snapshot via the FLO-422 helper ----
        from breakout_regime import compute_regime_snapshot
        snapshot = compute_regime_snapshot(
            ts=snap_ts,
            direction=direction,
            setup_type=setup_type,
            breakout_level=float(entry_price) if entry_price is not None else None,
            current_price=current_price,
            candles_m5=m5_candles,
            analyses_4h=analyses_4h,
            analyses_24h=analyses_24h,
            stage="author",
        )

        # ---- FLO-451: attach author-time multi_tf_indicators ----
        # The breakout snapshot above captures volatility-regime fields but NOT
        # the D1/H4 EMA stack. Snapshot the current multi-TF indicator state so
        # the Technical specialist voter can be retro-validated on this plan
        # later (faithful point-in-time HTF structure). Cheap read from
        # bot_state.json (the Brain's latest write); fail-soft -> None.
        try:
            _bs = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "data", "bot_state.json"
            )
            with open(_bs, "r", encoding="utf-8") as _f:
                snapshot["multi_tf_indicators"] = json.load(_f).get("multi_tf_indicators")
        except Exception:
            snapshot["multi_tf_indicators"] = None

        # ---- Persist via UPDATE ----
        _flo422_persist_snapshot(plan_id, snapshot)

        # ---- Single-line audit log. Stable precision for grep-friendliness:
        #      percentages -> 2 decimals; pip distances -> 1 decimal; counts/ints
        #      raw. None values render as "None" so absence is visible at a glance.
        def _fmt_pct(v): return f"{v:+.2f}%" if isinstance(v, (int, float)) else "None"
        warn_str = ",".join(snapshot.get("computation_warnings", []))
        log.info(
            f"BREAKOUT_REGIME_SNAPSHOT | plan={plan_id} | snapshot_version=1 | "
            f"stage=author | setup={setup_type} | dir={direction} | "
            f"impulse_total={snapshot.get('impulse_total_60m')} | "
            f"bb_width_4h={_fmt_pct(snapshot.get('bb_width_4h_pct'))} | "
            f"atr_4h={_fmt_pct(snapshot.get('atr_4h_pct'))} | "
            f"breakout_age_bars={snapshot.get('breakout_age_bars')} | "
            f"warnings=[{warn_str}]"
        )
    except Exception as e:
        # Fail-soft: snapshot is observability, must not break submission.
        try:
            log.warning(
                f"FLO-422 author snapshot failed for "
                f"{getattr(parsed_plan, 'id', '?')}: {type(e).__name__}: {e}"
            )
        except Exception:
            pass


def _flo422_fetch_m5_candles(ts: datetime, n: int) -> list:
    """Pull last `n` M5 candles ending at `ts`. Returns list of dicts with
    open/high/low/close keys. Empty list on any failure."""
    try:
        from mt5_safe import mt5, mt5_lock
        with mt5_lock:
            if not mt5.initialize():
                return []
            mt5.symbol_select("XAUUSD", True)
            ts_naive = ts.replace(tzinfo=None) if ts.tzinfo else ts
            rates = mt5.copy_rates_from("XAUUSD", mt5.TIMEFRAME_M5, ts_naive, n)
        if rates is None:
            return []
        out = []
        for r in rates:
            out.append({
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
            })
        return out
    except Exception:
        return []


def _flo422_fetch_analyses(ts: datetime, minutes_back: int) -> list:
    """Fetch `analyses` rows from history.db for the window
    [ts - minutes_back, ts]. Returns list of dicts. Empty list on failure."""
    try:
        import sqlite3
        import config as _cfg
        db_path = getattr(_cfg, "HISTORY_DB_PATH", "data/history.db")
        end = ts.replace(tzinfo=None) if ts.tzinfo else ts
        start = end - timedelta(minutes=minutes_back)
        conn = sqlite3.connect(db_path)
        try:
            cur = conn.cursor()
            cur.execute(
                """SELECT timestamp, current_price, atr_14, rsi_14, ema_50,
                          bb_upper, bb_middle, bb_lower, adx_14
                     FROM analyses
                    WHERE timestamp >= ? AND timestamp <= ?
                    ORDER BY timestamp""",
                (start.isoformat()[:19], end.isoformat()[:19]),
            )
            cols = ["timestamp", "current_price", "atr_14", "rsi_14", "ema_50",
                    "bb_upper", "bb_middle", "bb_lower", "adx_14"]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        finally:
            conn.close()
    except Exception:
        return []


def _flo422_persist_snapshot(plan_id: str, snapshot: dict) -> None:
    """UPDATE snow_plans.author_regime_snapshot_json. Fail-soft."""
    try:
        import sqlite3
        import config as _cfg
        db_path = getattr(_cfg, "HISTORY_DB_PATH", "data/history.db")
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "UPDATE snow_plans SET author_regime_snapshot_json = ? WHERE id = ?",
                (json.dumps(snapshot, default=str), plan_id),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        try:
            log.warning(f"FLO-422 snapshot persist failed for {plan_id}: {e}")
        except Exception:
            pass


# FLO-141: per-ticket adjustment rate limiter (in-memory, lost on restart)
_adjust_rate_history: Dict[int, List[float]] = {}


# FLO-432: DXY status snapshot cache (5-minute TTL, process-local)
_DXY_CACHE: Dict[str, Any] = {"payload": None, "fetched_at": 0.0}
_DXY_CACHE_TTL_SECS = 300.0  # 5 minutes


def _fetch_dxy_status_cached() -> Dict[str, Any]:
    """FLO-432 helper — fetch DXY snapshot with 5-min cache.

    Returns dict with: current, return_1d_pct, return_5d_pct,
    correlation_30d, signal (DXY_RISING/FALLING/NEUTRAL/UNKNOWN),
    symbol, fetched_at_iso.

    Signal thresholds: 5-day return > +0.75% → RISING; < -0.75% →
    FALLING; otherwise NEUTRAL. Tuned to gold's typical daily noise
    (~0.3-0.5% on DXY) so a single noisy day doesn't flip the label.

    Network failure / sparse history returns signal=DXY_UNKNOWN with
    an `error` field. Never raises.
    """
    import time as _time
    now = _time.time()
    cached = _DXY_CACHE.get("payload")
    if cached is not None and (now - _DXY_CACHE.get("fetched_at", 0)) < _DXY_CACHE_TTL_SECS:
        return dict(cached)  # defensive copy

    try:
        import yfinance as yf
    except Exception as e:
        return {
            "success": False,
            "signal": "DXY_UNKNOWN",
            "error": f"yfinance_unavailable: {e}",
        }

    # Pull 30 trading days of DXY history. Fall through symbols on failure.
    dxy_hist = None
    symbol_used = None
    for sym in ("DX-Y.NYB", "DX=F", "UUP"):
        try:
            ticker = yf.Ticker(sym)
            hist = ticker.history(period="45d")  # ~30 trading days
            if hist is None or hist.empty or len(hist) < 6:
                continue
            dxy_hist = hist
            symbol_used = sym
            break
        except Exception:
            continue

    if dxy_hist is None:
        payload = {
            "success": False,
            "signal": "DXY_UNKNOWN",
            "error": "dxy_history_unavailable_all_symbols",
        }
        _DXY_CACHE["payload"] = payload
        _DXY_CACHE["fetched_at"] = now
        return dict(payload)

    closes = dxy_hist["Close"].dropna()
    if len(closes) < 6:
        payload = {
            "success": False,
            "signal": "DXY_UNKNOWN",
            "error": "dxy_history_too_short",
        }
        _DXY_CACHE["payload"] = payload
        _DXY_CACHE["fetched_at"] = now
        return dict(payload)

    current = float(closes.iloc[-1])
    prev_1d = float(closes.iloc[-2])
    prev_5d = float(closes.iloc[-6]) if len(closes) >= 6 else None
    return_1d = ((current - prev_1d) / prev_1d) * 100 if prev_1d else 0.0
    return_5d = ((current - prev_5d) / prev_5d) * 100 if prev_5d else 0.0

    # 30-day correlation with gold
    corr_30d = None
    try:
        gold = yf.Ticker("GC=F").history(period="45d")
        if gold is not None and not gold.empty:
            g_closes = gold["Close"].dropna()
            # Align on common index
            common = closes.index.intersection(g_closes.index)
            if len(common) >= 10:
                d = closes.reindex(common).pct_change().dropna()
                g = g_closes.reindex(common).pct_change().dropna()
                common2 = d.index.intersection(g.index)
                if len(common2) >= 10:
                    corr_30d = round(float(d.reindex(common2).corr(g.reindex(common2))), 2)
    except Exception:
        corr_30d = None

    # Signal: 5-day return threshold
    if return_5d > 0.75:
        signal = "DXY_RISING"
    elif return_5d < -0.75:
        signal = "DXY_FALLING"
    else:
        signal = "DXY_NEUTRAL"

    from datetime import datetime, timezone
    payload = {
        "success": True,
        "current": round(current, 2),
        "return_1d_pct": round(return_1d, 2),
        "return_5d_pct": round(return_5d, 2),
        "correlation_30d": corr_30d,
        "signal": signal,
        "symbol": symbol_used,
        "fetched_at_iso": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "cache_ttl_secs": int(_DXY_CACHE_TTL_SECS),
    }
    _DXY_CACHE["payload"] = payload
    _DXY_CACHE["fetched_at"] = now
    return dict(payload)


def _today_realized_pnl_usd() -> float:
    """FLO-439 helper — sum profit of trades closed today (UTC).

    Reads history.db `trades` table. Returns 0.0 on any error (caller
    treats this as 'no realized loss yet today').
    """
    try:
        import sqlite3
        from datetime import datetime, timezone
        import os
        db_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "data", "history.db"
        )
        if not os.path.exists(db_path):
            return 0.0
        today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00")
        with sqlite3.connect(db_path) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT COALESCE(SUM(profit), 0.0) FROM trades WHERE close_time >= ?",
                (today_iso,),
            )
            row = cur.fetchone()
            return float(row[0]) if row else 0.0
    except Exception:
        return 0.0


def _mt5_tf(label: str):
    """FLO-438 helper — resolve timeframe label to mt5 const. None on failure."""
    try:
        from mt5_safe import mt5
    except Exception:
        return None
    return {
        "M1": getattr(mt5, "TIMEFRAME_M1", None),
        "M5": getattr(mt5, "TIMEFRAME_M5", None),
        "M15": getattr(mt5, "TIMEFRAME_M15", None),
        "M30": getattr(mt5, "TIMEFRAME_M30", None),
        "H1": getattr(mt5, "TIMEFRAME_H1", None),
        "H4": getattr(mt5, "TIMEFRAME_H4", None),
        "D1": getattr(mt5, "TIMEFRAME_D1", None),
    }.get(label)


def _scan_fvgs(tf_label: str, tf_const: Any, *, lookback: int = 100,
               max_results: int = 10) -> list:
    """FLO-438 — return up to `max_results` unfilled FVGs on `tf` (newest first).

    XAUUSD pip = 0.1 USD; 1 USD = 10 pips. Filled threshold is 50% of gap.
    """
    try:
        from mt5_safe import mt5, mt5_lock
        from datetime import datetime, timezone
    except Exception:
        return []
    try:
        with mt5_lock:
            rates = mt5.copy_rates_from_pos("XAUUSD", tf_const, 0, lookback + 5)
    except Exception:
        return []
    if rates is None or len(rates) < 5:
        return []

    fvgs = []
    n = len(rates)
    for i in range(n - 3):
        c0 = rates[i]
        c2 = rates[i + 2]
        c0_high = float(c0["high"])
        c0_low = float(c0["low"])
        c2_high = float(c2["high"])
        c2_low = float(c2["low"])

        if c0_high < c2_low:
            direction = "bullish"
            bottom = c0_high
            top = c2_low
        elif c0_low > c2_high:
            direction = "bearish"
            bottom = c2_high
            top = c0_low
        else:
            continue

        gap_size = top - bottom
        if gap_size <= 0:
            continue
        midpoint = (top + bottom) / 2.0

        # Filled: scan subsequent candles for retracement >= 50%
        filled_pct = 0.0
        for j in range(i + 3, n):
            r = rates[j]
            if direction == "bullish":
                low_j = float(r["low"])
                if low_j < bottom:
                    filled_pct = 100.0
                    break
                if low_j < top:
                    pct = (top - low_j) / gap_size * 100.0
                    if pct > filled_pct:
                        filled_pct = pct
            else:
                high_j = float(r["high"])
                if high_j > top:
                    filled_pct = 100.0
                    break
                if high_j > bottom:
                    pct = (high_j - bottom) / gap_size * 100.0
                    if pct > filled_pct:
                        filled_pct = pct
        if filled_pct >= 50.0:
            continue

        formed_ts = datetime.fromtimestamp(int(c2["time"]), tz=timezone.utc)
        fvgs.append({
            "direction": direction,
            "top": round(top, 2),
            "bottom": round(bottom, 2),
            "midpoint": round(midpoint, 2),
            "size_pips": round(gap_size * 10, 1),
            "timeframe": tf_label,
            "age_candles": n - 1 - (i + 2),
            "filled_pct": round(filled_pct, 1),
            "formed_at_iso": formed_ts.isoformat().replace("+00:00", "Z"),
        })

    fvgs.sort(key=lambda f: f["age_candles"])
    return fvgs[:max_results]


def _scan_sweeps(tf_label: str, tf_const: Any, *, lookback: int = 100,
                 max_results: int = 10, fractal_window: int = 3) -> list:
    """FLO-438 — return recent liquidity sweeps (newest first).

    A sweep = a candle whose wick pierces a prior swing high/low but
    whose close is back inside. Fractal swing: a high/low is a swing
    if it is the extreme over [i-fractal_window, i+fractal_window].
    """
    try:
        from mt5_safe import mt5, mt5_lock
        from datetime import datetime, timezone
    except Exception:
        return []
    try:
        with mt5_lock:
            rates = mt5.copy_rates_from_pos("XAUUSD", tf_const, 0, lookback + 5)
    except Exception:
        return []
    if rates is None or len(rates) < 2 * fractal_window + 2:
        return []

    n = len(rates)
    # Identify swing highs and lows
    swing_highs: list = []
    swing_lows: list = []
    for i in range(fractal_window, n - fractal_window):
        h_i = float(rates[i]["high"])
        l_i = float(rates[i]["low"])
        window_high = max(float(rates[j]["high"]) for j in range(i - fractal_window, i + fractal_window + 1))
        window_low = min(float(rates[j]["low"]) for j in range(i - fractal_window, i + fractal_window + 1))
        if h_i == window_high:
            swing_highs.append((i, h_i))
        if l_i == window_low:
            swing_lows.append((i, l_i))

    sweeps = []
    # Sweep high: candle k > some swing_high.idx has wick above swing_high but close <= swing_high
    for (s_idx, s_high) in swing_highs:
        for k in range(s_idx + fractal_window + 1, n):
            r = rates[k]
            high_k = float(r["high"])
            close_k = float(r["close"])
            if high_k > s_high and close_k <= s_high:
                wick_pips = (high_k - s_high) * 10
                if wick_pips < 1.0:
                    continue
                # recovered_pct = how much of the breach was given back at close
                breach_size = high_k - s_high
                recovered = (high_k - close_k) / breach_size * 100.0 if breach_size > 0 else 100.0
                formed_ts = datetime.fromtimestamp(int(r["time"]), tz=timezone.utc)
                sweeps.append({
                    "level": round(s_high, 2),
                    "direction": "BSL",
                    "sweep_candle_time_iso": formed_ts.isoformat().replace("+00:00", "Z"),
                    "wick_size_pips": round(wick_pips, 1),
                    "recovered_pct": round(min(recovered, 100.0), 1),
                    "age_candles": n - 1 - k,
                    "timeframe": tf_label,
                })
                break  # one sweep per swing high (the first one)

    for (s_idx, s_low) in swing_lows:
        for k in range(s_idx + fractal_window + 1, n):
            r = rates[k]
            low_k = float(r["low"])
            close_k = float(r["close"])
            if low_k < s_low and close_k >= s_low:
                wick_pips = (s_low - low_k) * 10
                if wick_pips < 1.0:
                    continue
                breach_size = s_low - low_k
                recovered = (close_k - low_k) / breach_size * 100.0 if breach_size > 0 else 100.0
                formed_ts = datetime.fromtimestamp(int(r["time"]), tz=timezone.utc)
                sweeps.append({
                    "level": round(s_low, 2),
                    "direction": "SSL",
                    "sweep_candle_time_iso": formed_ts.isoformat().replace("+00:00", "Z"),
                    "wick_size_pips": round(wick_pips, 1),
                    "recovered_pct": round(min(recovered, 100.0), 1),
                    "age_candles": n - 1 - k,
                    "timeframe": tf_label,
                })
                break

    sweeps.sort(key=lambda s: s["age_candles"])
    return sweeps[:max_results]


class AgentTools:
    def __init__(
        self,
        bot: Any,
        *,
        executor: Any,
        safety_checks_module: Any,
        risk_manager_module: Any,
    ):
        self._bot = bot
        self._executor = executor
        self._safety = safety_checks_module
        self._risk = risk_manager_module
        # FLO-382 D1: per-cycle Recipe Book pull buffer. Appended on
        # successful get_snow_recipe_book invocations; filtered by
        # recency window on submit_plan_to_snow emit (NOT cleared)
        # so paired_hedge cycles that submit two plans in the same
        # second both see the cycle's pulls.
        from collections import deque as _deque
        self._recipe_pulls: "_deque[dict]" = _deque(maxlen=50)
        # FLO-393: orthogonal per-Floki-cycle counter. Reset to 0 at
        # the top of `agent_decide()` (canonical cycle start). Read by
        # `submit_plan_to_snow` to enforce mandatory Recipe Book
        # consultation. Coexists with the FLO-382 deque above without
        # interference — the deque keeps its 600s recency telemetry,
        # this counter is a hard gate.
        self._recipe_pulls_count: int = 0

    def set_next_check(self, minutes: int = 5) -> Dict[str, Any]:
        start = time.time()
        try:
            m = self._safe_int(minutes)
            if m is None:
                m = 5
            _original_requested = int(m)

            # FLO-419 Phase 2 (CEO directive 2026-05-01): floor raised
            # 30 -> 60. Cycle is synchronised to the H1 candle close
            # (snapped below). Faster cadence (10-min) still allowed
            # only when NO plan AND NO position exists — fresh-authoring
            # fast-iteration window. Conservative direction on lookup
            # failure: keep the 60-min floor.
            _floor = 60
            try:
                from snow import db as _snow_db
                _no_plan = not _snow_db.list_plans_by_status(
                    ("pending", "active"), limit=1,
                )
            except Exception:
                _no_plan = False  # conservative — assume plan exists
            try:
                _positions = self._executor.get_open_positions() if self._executor else []
                _no_position = not _positions
            except Exception:
                _no_position = False  # conservative — assume position exists
            if _no_plan and _no_position:
                _floor = 10

            # FLO-297: Range clamp (valid 2-120). Previously silent.
            _range_clamped = False
            if m < _floor:
                m = _floor
                _range_clamped = True
            elif m > 120:
                m = 120
                _range_clamped = True

            # FLO-403 Phase 1 — legacy `FLOKI_MAX_CHECK_WITH_POSITION` cap
            # removed. That cap forced fast re-checks when a position was
            # open (10-min default) under the pre-FLO-403 model where
            # Floki managed open trades. Under Phase 1, Floki does NOT
            # manage open trades — Snow contingencies + monitor.py handle
            # them — so a 10-min cap below the new 30-min floor would
            # silently re-introduce the cost driver Phase 1 is removing.
            # The 30-min floor (above) IS the policy with a position open.
            _position_capped = False
            _max_pos = 0  # retained for clamp-reason payload symmetry only

            now = datetime.utcnow()
            next_at = now + timedelta(minutes=int(m))

            # FLO-419 Phase 2 (CEO directive 2026-05-01): H1 synchronisation.
            # Cycles >= 30 min are normal-cadence and must land at minute 1
            # of an hour (1 minute after the H1 candle closes) so Floki
            # always analyses with a COMPLETE H1 candle in hand. Cycles
            # < 30 min are emergency/news-driven shorts and bypass the snap.
            # The snap pushes next_at FORWARD to the next XX:01 boundary;
            # the actual delay may exceed Floki's request by up to 60
            # minutes when a request lands mid-hour. The response payload
            # surfaces both the original request and the snapped time.
            _h1_synced = False
            if int(m) >= 30:
                _snapped = next_at.replace(minute=1, second=0, microsecond=0)
                if _snapped < next_at:
                    _snapped = _snapped + timedelta(hours=1)
                if _snapped != next_at:
                    next_at = _snapped
                    _h1_synced = True

            # FLO-419 Phase 2: 21:00-22:00 UTC daily break window. Never
            # schedule a cycle inside this hour — broker/data systems may
            # be in maintenance and there's no fresh H1 candle to analyse.
            # Push to 22:01 UTC. Applies regardless of cycle length.
            _break_dodged = False
            if next_at.hour == 21:
                next_at = next_at.replace(hour=22, minute=1, second=0, microsecond=0)
                _break_dodged = True

            payload = {
                "next_check_at": next_at.isoformat(timespec="seconds") + "Z",
                "requested_minutes": int(m),
            }
            if _h1_synced:
                payload["h1_synced"] = True
            if _break_dodged:
                payload["break_dodged"] = True

            ok = self._write_json_atomic(self._next_check_path(), payload)
            if not ok:
                self._log_fail("set_next_check", start, "persist failed")
                return {"success": False, "reason": "persist failed"}

            self._log_tool("set_next_check", start, f"minutes={m}")
            result = {"success": True, **payload}

            # FLO-297: Report all clamping paths so Floki sees what happened
            if _range_clamped or _position_capped:
                result["clamped_from"] = _original_requested
                reasons = []
                if _range_clamped:
                    reasons.append("out_of_range (valid 2-120)")
                if _position_capped:
                    reasons.append(f"position_open (max {_max_pos}m)")
                result["clamp_reason"] = " + ".join(reasons)
            return result
        except Exception as e:
            self._log_tool("set_next_check", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    # ---------------------------------------------------------------------
    # Internals
    # ---------------------------------------------------------------------

    def _no_cache(self) -> Dict[str, Any]:
        return {"success": False, "reason": "no cached data available"}

    def _last_agent_data(self) -> Optional[Dict[str, Any]]:
        try:
            dp = getattr(self._bot, "_last_agent_data", None)
            return dp if isinstance(dp, dict) and dp else None
        except Exception:
            return None

    def _nearest_sr_zones(self, zones: List[Dict[str, Any]], mid_price: float, limit: int = 5) -> List[Dict[str, Any]]:
        try:
            def zone_center(z: Dict[str, Any]) -> Optional[float]:
                a = self._safe_float(z.get("low"))
                b = self._safe_float(z.get("high"))
                if a is not None and b is not None:
                    return (a + b) / 2.0
                c = self._safe_float(z.get("level"))
                if c is not None:
                    return c
                c = self._safe_float(z.get("price"))
                if c is not None:
                    return c
                return None

            scored: List[Tuple[float, Dict[str, Any]]] = []
            for z in zones:
                if not isinstance(z, dict):
                    continue
                c = zone_center(z)
                if c is None:
                    continue
                scored.append((abs(float(c) - float(mid_price)), z))
            scored.sort(key=lambda x: x[0])
            return [z for _, z in scored[: max(1, int(limit))]]
        except Exception:
            return zones[: max(1, int(limit))] if isinstance(zones, list) else []

    def _extract_ema50_ema200(self, dp: Dict[str, Any]) -> Dict[str, Any]:
        out = {"ema50": None, "ema200": None}
        try:
            ind = dp.get("indicators") or {}
            emas = ind.get("emas") or {}
            if isinstance(emas, dict):
                out["ema50"] = self._safe_float(emas.get("ema50"))
                out["ema200"] = self._safe_float(emas.get("ema200"))
        except Exception:
            return out
        return out

    def _extract_recent_candles_for_rex(self, dp: Dict[str, Any]) -> Dict[str, Any]:
        out = {"H1_last5": [], "M5_last3": [], "volume_context": {"H1_last": None, "M5_last3": []}}
        try:
            cds = dp.get("candles") or {}
            if isinstance(cds, dict):
                h1 = cds.get("H1")
                m5 = cds.get("M5")
                if isinstance(h1, list) and h1:
                    out["H1_last5"] = h1[-5:]
                if isinstance(m5, list) and m5:
                    out["M5_last3"] = m5[-3:]
        except Exception:
            pass

        try:
            if not out["H1_last5"]:
                built = self.get_candles("H1", 5)
                if isinstance(built, dict) and isinstance(built.get("candles"), list):
                    out["H1_last5"] = built.get("candles")[-5:]
        except Exception:
            pass

        try:
            if not out["M5_last3"]:
                built = self.get_candles("M5", 3)
                if isinstance(built, dict) and isinstance(built.get("candles"), list):
                    out["M5_last3"] = built.get("candles")[-3:]
        except Exception:
            pass

        try:
            if isinstance(out.get("H1_last5"), list) and out["H1_last5"]:
                out["volume_context"]["H1_last"] = out["H1_last5"][-1].get("volume")
        except Exception:
            pass

        try:
            vols = []
            for c in out.get("M5_last3") or []:
                if isinstance(c, dict):
                    vols.append(c.get("volume"))
            out["volume_context"]["M5_last3"] = vols
        except Exception:
            pass

        return out

    def _last_df(self) -> Any:
        try:
            return getattr(self._bot, "_last_df", None)
        except Exception:
            return None

    def _now_iso(self) -> str:
        return datetime.utcnow().isoformat(timespec="seconds") + "Z"

    def _safe_float(self, v: Any) -> Optional[float]:
        try:
            if v is None:
                return None
            return float(v)
        except Exception:
            return None

    def _safe_int(self, v: Any) -> Optional[int]:
        try:
            if v is None:
                return None
            return int(v)
        except Exception:
            return None

    def _infer_session_from_utc_hour(self, utc_hour: Optional[int]) -> Optional[str]:
        if utc_hour is None:
            return None
        try:
            h = int(utc_hour) % 24
        except Exception:
            return None
        if 0 <= h <= 6:
            return "ASIAN"
        if 7 <= h <= 12:
            return "LONDON"
        if 13 <= h <= 20:
            return "NY"
        return "OFF"

    def _build_session_context_for_rex(
        self, session_name: Optional[str], indicators: Dict[str, Any], dp: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Build session context so Rex can evaluate data in session context."""
        ctx: Dict[str, Any] = {"name": session_name}
        try:
            utc_hour = self._safe_int(dp.get("utc_hour"))
            if utc_hour is None:
                utc_hour = datetime.utcnow().hour
            ctx["utc_hour"] = utc_hour

            # Hours into session
            session_starts = {"ASIAN": 0, "LONDON": 7, "NY": 13, "OFF": 21}
            start = session_starts.get(session_name or "", 0)
            ctx["hours_into_session"] = (utc_hour - start) % 24

            # Volume ratio vs average (from indicators if available)
            vol = indicators.get("volume") if isinstance(indicators, dict) else None
            if isinstance(vol, dict):
                ctx["volume_ratio"] = vol.get("tick_volume_ratio")
                ctx["volume_classification"] = vol.get("classification")
        except Exception:
            pass
        return ctx

    def _rsi_bucket(self, rsi: Optional[float]) -> Optional[str]:
        if rsi is None:
            return None
        try:
            v = float(rsi)
        except Exception:
            return None
        if v < 30:
            return "<30"
        if v < 40:
            return "30-40"
        if v <= 60:
            return "40-60"
        if v <= 70:
            return "60-70"
        return ">70"

    def _extract_context_for_patterns(self) -> Dict[str, Any]:
        dp = self._last_agent_data() or {}

        direction = None
        session = None
        rsi = None

        try:
            direction = dp.get("decision")
            if isinstance(direction, str) and direction.upper() in ("BUY", "SELL"):
                direction = direction.upper()
            else:
                direction = None
        except Exception:
            direction = None

        try:
            session = dp.get("session_name")
            if not isinstance(session, str) or not session.strip():
                session = None
            else:
                session = session.strip().upper()
        except Exception:
            session = None

        if session is None:
            try:
                utc_hour = self._safe_int(dp.get("utc_hour"))
                session = self._infer_session_from_utc_hour(utc_hour)
            except Exception:
                session = None

        try:
            ind = dp.get("indicators") or {}
            rsi_blob = ind.get("rsi") or {}
            rsi = self._safe_float(rsi_blob.get("value"))
        except Exception:
            rsi = None

        return {
            "direction": direction,
            "session": session,
            "rsi": rsi,
            "rsi_bucket": self._rsi_bucket(rsi),
        }

    def _get_connection(self):
        import sqlite3
        import config

        db_path = os.path.abspath(getattr(config, "HISTORY_DB_PATH", "data/history.db"))
        conn = sqlite3.connect(db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        return conn

    def _find_nearest_analysis(self, conn, open_time: str):
        from datetime import datetime

        if not open_time:
            return None
        try:
            cur = conn.execute(
                """
                SELECT timestamp, utc_hour, session_name, rsi_14
                FROM analyses
                WHERE timestamp <= ?
                ORDER BY timestamp DESC
                LIMIT 1
                """,
                (open_time,),
            )
            row = cur.fetchone()
            if not row:
                return None

            t_trade = datetime.fromisoformat(str(open_time).replace("Z", ""))
            t_ana = datetime.fromisoformat(str(row["timestamp"]).replace("Z", ""))
            gap = (t_trade - t_ana).total_seconds()
            if gap < 0 or gap > 5 * 60:
                return None
            return row
        except Exception:
            return None

    def _query_similar_losing_trades(self, context: Dict[str, Any], limit: int = 2) -> List[Dict[str, Any]]:
        direction = str(context.get("direction") or "").upper().strip()
        session = str(context.get("session") or "").upper().strip()
        rsi_bucket = context.get("rsi_bucket")

        if direction not in ("BUY", "SELL"):
            return []

        conn = None
        try:
            conn = self._get_connection()
            cur = conn.execute(
                """
                SELECT ticket, direction, profit, open_price, close_price, open_time, close_time, close_reason
                FROM trades
                WHERE close_time IS NOT NULL
                  AND profit IS NOT NULL
                  AND profit < 0
                  AND UPPER(direction) = ?
                  AND decision_source IN ('floki_agent', 'agent_floki')
                ORDER BY close_time DESC
                LIMIT 50
                """,
                (direction,),
            )
            candidates = list(cur.fetchall() or [])

            filtered: List[Dict[str, Any]] = []
            for tr in candidates:
                try:
                    open_time = str(tr["open_time"] or "")
                    a = self._find_nearest_analysis(conn, open_time=open_time)

                    a_session = None
                    a_rsi = None
                    if a is not None:
                        a_session = str(a["session_name"] or "").strip().upper() or None
                        a_rsi = self._safe_float(a["rsi_14"])
                        if a_session is None:
                            a_session = self._infer_session_from_utc_hour(self._safe_int(a["utc_hour"]))

                    if session and a_session and session != a_session:
                        continue

                    if rsi_bucket and a_rsi is not None:
                        if self._rsi_bucket(a_rsi) != rsi_bucket:
                            continue

                    filtered.append(
                        {
                            "ticket": int(tr["ticket"] or 0),
                            "direction": str(tr["direction"] or ""),
                            "profit": float(tr["profit"] or 0.0),
                            "open_price": self._safe_float(tr["open_price"]),
                            "close_price": self._safe_float(tr["close_price"]),
                            "open_time": open_time,
                            "close_time": str(tr["close_time"] or ""),
                            "close_reason": str(tr["close_reason"] or ""),
                            "session": a_session,
                            "rsi_14": a_rsi,
                        }
                    )

                    if len(filtered) >= int(limit):
                        break
                except Exception:
                    continue

            return filtered
        except Exception:
            return []
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _pip_size(self) -> float:
        return 0.1

    def _sl_pips_from_prices(self, entry: float, sl: float) -> Optional[float]:
        try:
            pip = self._pip_size()
            return abs(entry - sl) / pip
        except Exception:
            return None

    def _extract_price_from_cache(self, dp: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        try:
            cp = dp.get("current_price") or {}
            bid = self._safe_float(cp.get("bid"))
            ask = self._safe_float(cp.get("ask"))
            spread = self._safe_float(cp.get("spread"))

            if bid is None or ask is None:
                return None

            # Bug C live-recovery: the cache may have been coerced to a
            # placeholder upstream (bid == ask via main.py:2190 scalar coerce,
            # or spread == 0.0 hardcoded via main.py:6139). Refresh from MT5
            # once if the trigger fires; on any failure, pass cache values
            # through unchanged (never raise, never block).
            if bid == ask or spread is None or spread <= 0.0:
                live = self._fetch_live_price_from_executor()
                if live and live.get("bid") != live.get("ask"):
                    bid = live["bid"]
                    ask = live["ask"]
                    spread = live["spread"]

            if spread is None:
                spread = (ask - bid) / 0.1  # Convert raw price diff to pips (gold pip = 0.1)

            ts = cp.get("timestamp") or dp.get("timestamp") or self._now_iso()
            return {
                "bid": bid,
                "ask": ask,
                "spread": spread,
                "timestamp": ts,
            }
        except Exception:
            return None

    def _fetch_live_price_from_executor(self) -> Optional[Dict[str, Any]]:
        """Bug C live-recovery helper (dormant until Commit 2 wiring).

        Fetches a fresh (bid, ask) tuple directly from MT5 via the executor,
        intended as a fallback when the cached price dict has been coerced
        to a placeholder (bid == ask or spread <= 0.0, see main.py:2190 /
        main.py:6139 injection sites). Returns a {bid, ask, spread} dict on
        success or None on any failure. Caller is responsible for deciding
        whether to accept the result (e.g. by checking bid != ask); helper
        does not second-guess live MT5. Never raises.
        """
        try:
            if self._executor is None:
                return None
            tup = self._executor.get_current_price()
            if not tup or len(tup) != 2:
                return None
            bid = self._safe_float(tup[0])
            ask = self._safe_float(tup[1])
            if bid is None or ask is None:
                return None
            spread = (ask - bid) / 0.1  # XAU/USD: 1 pip = 0.1
            return {"bid": bid, "ask": ask, "spread": spread}
        except Exception as e:
            try:
                log.debug(f"agent_tools: _fetch_live_price_from_executor non-blocking error: {e}")
            except Exception:
                pass
            return None

    def _log_tool(self, name: str, start_t: float, extra: str = "") -> None:
        """Audit-log a tool call via the project TradingLogger.

        WARNING — blast radius: `log` is `logger.TradingLogger`, whose
        FileHandler writes to `logs/trading_bot_YYYY-MM-DD.log`. That is
        the SAME file the running production bot writes to. Any test
        that instantiates AgentTools and reaches this method will pollute
        the daily log with entries that are visually indistinguishable
        from real Floki tool calls — caused a false-positive P0 on
        FLO-347 Phase 6.5 evidence window. `snow/tests/conftest.py`
        installs a session-scoped fixture that redirects TradingLogger's
        FileHandler to a tmp path for pytest runs; when adding new tests
        that hit `_log_tool`, verify that fixture is in effect or tests
        will silently re-pollute `logs/trading_bot_*.log`.
        """
        try:
            ms = int((time.time() - start_t) * 1000)
            if extra:
                log.info(f"AGENT_TOOL | {name} | {ms}ms | {extra}")
            else:
                log.info(f"AGENT_TOOL | {name} | {ms}ms")
        except Exception:
            pass

    def _log_no_cache(self, name: str, start_t: float, extra: str = "") -> None:
        try:
            msg = "no_cache"
            if extra:
                msg = f"{msg} | {extra}"
            self._log_tool(name, start_t, msg)
        except Exception:
            pass

    def _log_fail(self, name: str, start_t: float, reason: str) -> None:
        try:
            r = str(reason or "").strip()
            self._log_tool(name, start_t, f"fail | {r}" if r else "fail")
        except Exception:
            pass

    # ---------------------------------------------------------------------
    # Market data tools (cache-only)
    # ---------------------------------------------------------------------

    def get_current_price(self) -> Dict[str, Any]:
        start = time.time()
        try:
            dp = self._last_agent_data()
            if not dp:
                self._log_no_cache("get_current_price", start)
                return self._no_cache()

            out = self._extract_price_from_cache(dp)
            if not out:
                self._log_no_cache("get_current_price", start)
                return self._no_cache()

            self._log_tool("get_current_price", start, f"bid={out.get('bid')} ask={out.get('ask')}")
            return out
        except Exception as e:
            self._log_tool("get_current_price", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    def get_position_events(self) -> Dict[str, Any]:
        start = time.time()
        try:
            path = self._agent_monitor_events_path()
            if not os.path.exists(path):
                self._log_tool("get_position_events", start, "empty")
                return {"events": []}

            try:
                import json

                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                self._log_tool("get_position_events", start, "read_failed")
                return {"events": []}

            if not isinstance(data, list):
                data = []

            events = data[-20:]
            self._log_tool("get_position_events", start, f"count={len(events)}")
            return {"events": events}
        except Exception as e:
            self._log_tool("get_position_events", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    # ---------------------------------------------------------------------
    # Position management tools
    # ---------------------------------------------------------------------

    def set_watch_conditions(self, ticket: int, conditions: List[Dict[str, Any]] = None) -> Dict[str, Any]:
        start = time.time()
        try:
            try:
                t = int(ticket)
            except Exception:
                return {"success": False, "reason": "invalid ticket"}

            if not isinstance(conditions, list) or not conditions:
                self._log_tool("set_watch_conditions", start, f"ticket={t} | missing conditions arg")
                return {
                    "success": False,
                    "reason": "conditions argument required",
                    "hint": "Pass conditions as array of objects, e.g.: conditions=[{type:'pnl_threshold', value:-15}, {type:'price_touch', level:4550}]",
                }

            # FLO-301: helper validates a single leaf condition for both top-level
            # conditions and inside 'all_of' compound conditions.
            def _clean_leaf(c: Dict[str, Any]) -> Optional[Dict[str, Any]]:
                if not isinstance(c, dict):
                    return None
                ctype = str(c.get("type", "")).strip()
                desc = str(c.get("description", "")).strip()
                if not ctype:
                    return None
                if ctype == "price_touch":
                    lvl = self._safe_float(c.get("level"))
                    if lvl is None:
                        return None
                    return {"type": "price_touch", "level": float(lvl), "description": desc}
                if ctype == "pnl_threshold":
                    v = self._safe_float(c.get("value"))
                    if v is None:
                        return None
                    return {"type": "pnl_threshold", "value": float(v), "description": desc}
                if ctype == "pnl_below":
                    v = self._safe_float(c.get("value"))
                    if v is None:
                        return None
                    return {"type": "pnl_below", "value": float(v), "description": desc}
                if ctype == "pnl_above":
                    lvl = self._safe_float(c.get("level"))
                    if lvl is None:
                        lvl = self._safe_float(c.get("value"))
                    if lvl is None:
                        return None
                    return {"type": "pnl_above", "level": float(lvl), "description": desc}
                if ctype == "bb_position":
                    want = str(c.get("value", "")).strip().lower()
                    if want not in ("above_upper", "below_lower", "upper_band", "lower_band", "middle"):
                        return None
                    return {"type": "bb_position", "value": want, "description": desc}
                if ctype == "mfe_drawdown":
                    pct = self._safe_float(c.get("pct"))
                    if pct is None or pct <= 0 or pct > 100:
                        return None
                    return {"type": "mfe_drawdown", "pct": float(pct), "description": desc}
                if ctype == "indicator_threshold":
                    ind = str(c.get("indicator", "")).strip().lower()
                    direction = str(c.get("direction", "")).strip().lower()
                    level = self._safe_float(c.get("level"))
                    if level is None or direction not in ("above", "below"):
                        return None
                    if ind not in ("vix", "rsi", "macd_histogram", "macd_hist", "adx"):
                        return None
                    return {
                        "type": "indicator_threshold",
                        "indicator": ind,
                        "level": float(level),
                        "direction": direction,
                        "description": desc,
                    }
                return None

            cleaned: List[Dict[str, Any]] = []
            for c in conditions:
                if not isinstance(c, dict):
                    continue

                # FLO-301: compound 'all_of' condition with action dispatch.
                if isinstance(c.get("all_of"), list) and c.get("all_of"):
                    action = str(c.get("action", "wake")).strip().lower()
                    if action not in ("wake", "close", "adjust_sl"):
                        continue
                    sub_cleaned: List[Dict[str, Any]] = []
                    for sub in c["all_of"]:
                        sc = _clean_leaf(sub)
                        if sc is not None:
                            sub_cleaned.append(sc)
                    if not sub_cleaned:
                        continue
                    entry: Dict[str, Any] = {
                        "all_of": sub_cleaned,
                        "action": action,
                        "description": str(c.get("description", "")).strip(),
                        "fired_at": None,
                    }
                    if action == "adjust_sl":
                        sl_val = self._safe_float(c.get("sl_value"))
                        if sl_val is None:
                            continue  # adjust_sl requires sl_value
                        entry["sl_value"] = float(sl_val)
                    cleaned.append(entry)
                    continue

                # Legacy single-condition path (always action=wake).
                leaf = _clean_leaf(c)
                if leaf is not None:
                    cleaned.append(leaf)

            if not cleaned:
                return {"success": False, "reason": "no valid conditions"}

            store = self._load_watch_conditions()
            # FLO-301: preserve existing mfe_pnl tracking across re-sets.
            _existing = store.get(str(t)) if isinstance(store.get(str(t)), dict) else {}
            _entry: Dict[str, Any] = {
                "updated_at": utc_iso(),  # FLO-286
                "conditions": cleaned,
            }
            if "mfe_pnl" in _existing:
                _entry["mfe_pnl"] = _existing["mfe_pnl"]
            store[str(t)] = _entry

            ok = self._write_json_atomic(self._watch_conditions_path(), store)
            if not ok:
                return {"success": False, "reason": "persist failed"}

            self._log_tool("set_watch_conditions", start, f"ticket={t} count={len(cleaned)}")
            resp: Dict[str, Any] = {"success": True, "ticket": t, "count": len(cleaned)}
            # FLO-302: warn Floki when bb_position is set to values that require
            # numeric indicator plumbing not yet available. above_upper/below_lower
            # will silently no-fire today; upper_band/lower_band/middle work.
            def _uses_numeric_bb(c: Dict[str, Any]) -> bool:
                if c.get("type") == "bb_position" and str(c.get("value", "")).lower() in ("above_upper", "below_lower"):
                    return True
                for sub in c.get("all_of", []) or []:
                    if isinstance(sub, dict) and sub.get("type") == "bb_position" and str(sub.get("value", "")).lower() in ("above_upper", "below_lower"):
                        return True
                return False
            if any(_uses_numeric_bb(c) for c in cleaned):
                resp["warnings"] = [
                    "bb_position values 'above_upper' and 'below_lower' require numeric "
                    "BB position data not yet in the indicator pipeline — these will NOT fire "
                    "at runtime (FLO-302 pending). Use 'upper_band' / 'lower_band' / 'middle' "
                    "which map to the current categorical bb_position field."
                ]
            return resp
        except Exception as e:
            self._log_tool("set_watch_conditions", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    def set_wake_conditions(self, max_sleep_minutes: int, conditions: List[Dict[str, Any]]) -> Dict[str, Any]:
        start = time.time()
        try:
            try:
                msm = int(max_sleep_minutes)
            except Exception:
                return {"success": False, "reason": "invalid max_sleep_minutes"}

            if msm <= 0:
                return {"success": False, "reason": "max_sleep_minutes must be positive"}

            if not isinstance(conditions, list) or not conditions:
                return {"success": False, "reason": "conditions must be a non-empty list"}

            allowed_types = {
                "price_above",
                "price_below",
                "price_touch",
                "h1_volume_above",
                "scanner_pattern",
                "indicator_above",
                "indicator_below",
            }

            cleaned: List[Dict[str, Any]] = []
            for idx, c in enumerate(conditions, start=1):
                if not isinstance(c, dict):
                    continue

                ctype = str(c.get("type", "")).strip()
                if not ctype or ctype not in allowed_types:
                    continue

                desc = str(c.get("description", "")).strip()
                cid = str(c.get("id") or "").strip() or f"c{idx}"

                if ctype in ("price_above", "price_below", "price_touch"):
                    lvl = self._safe_float(c.get("level"))
                    if lvl is None:
                        continue
                    cleaned.append({"id": cid, "type": ctype, "level": float(lvl), "description": desc})
                elif ctype == "h1_volume_above":
                    thr = self._safe_float(c.get("threshold"))
                    if thr is None:
                        continue
                    cleaned.append({"id": cid, "type": ctype, "threshold": float(thr), "description": desc})
                elif ctype == "scanner_pattern":
                    pat = str(c.get("pattern") or "").strip()
                    if not pat:
                        continue
                    cleaned.append({"id": cid, "type": ctype, "pattern": pat, "description": desc})
                elif ctype in ("indicator_above", "indicator_below"):
                    ind = str(c.get("indicator") or "").strip().lower()
                    thr = self._safe_float(c.get("threshold"))
                    if not ind or thr is None:
                        continue
                    cleaned.append({"id": cid, "type": ctype, "indicator": ind, "threshold": float(thr), "description": desc})

            if not cleaned:
                return {"success": False, "reason": "no valid conditions"}

            # FLO-204: Preserve fired_ids for conditions with same ID AND same value.
            # If Floki re-sets the same conditions after being woken, already-fired
            # conditions stay fired. If Floki changes a value (e.g., price_below from
            # 4654 to 4622), the condition is treated as new and will trigger.
            preserved_fired = []
            try:
                wc_path = self._wake_conditions_path()
                if os.path.exists(wc_path):
                    with open(wc_path, "r", encoding="utf-8") as f:
                        old_wc = json.loads(f.read())
                    old_fired = set(str(x) for x in (old_wc.get("fired_ids") or []) if x)
                    if old_fired:
                        # Build lookup: id → signature (type + level/threshold/pattern)
                        old_conds = {str(c.get("id")): c for c in (old_wc.get("conditions") or []) if isinstance(c, dict)}
                        for nc in cleaned:
                            nid = str(nc.get("id"))
                            if nid not in old_fired:
                                continue
                            oc = old_conds.get(nid)
                            if not oc:
                                continue
                            # Same ID — check if value also matches
                            same = (nc.get("type") == oc.get("type")
                                    and nc.get("level") == oc.get("level")
                                    and nc.get("threshold") == oc.get("threshold")
                                    and nc.get("pattern") == oc.get("pattern"))
                            if same:
                                preserved_fired.append(nid)
            except Exception:
                preserved_fired = []

            now_iso = utc_iso()  # FLO-286
            payload = {
                "updated_at": now_iso,
                "sleep_started_at": now_iso,
                "max_sleep_minutes": msm,
                "conditions": cleaned,
            }
            if preserved_fired:
                payload["fired_ids"] = preserved_fired

            ok = self._write_json_atomic(self._wake_conditions_path(), payload)
            if not ok:
                return {"success": False, "reason": "persist failed"}

            # Sync price-level conditions to EA for tick-level monitoring + chart lines
            try:
                import config as _cfg_ea
                _fired_set = set(str(x) for x in (payload.get("fired_ids") or []))
                _ea_alerts = []
                for c in cleaned:
                    if c.get("type") in ("price_above", "price_below", "price_touch") and c.get("level") is not None:
                        if str(c.get("id", "")) not in _fired_set:
                            _ea_alerts.append({"id": str(c["id"]), "type": c["type"], "level": float(c["level"])})
                _ea_payload = {
                    "version": 1,
                    "timestamp": utc_iso(),  # FLO-309
                    "alerts": _ea_alerts,
                }
                _ea_path = _cfg_ea.PRICE_ALERTS_JSON_PATH
                _ea_tmp = _ea_path + ".tmp"
                with open(_ea_tmp, "w", encoding="utf-8") as f:
                    json.dump(_ea_payload, f, ensure_ascii=False, indent=2)
                os.replace(_ea_tmp, _ea_path)
            except Exception:
                pass

            try:
                from db_writer import record_agent_event

                def _fmt_minutes(m: int) -> str:
                    try:
                        m_i = int(m)
                    except Exception:
                        m_i = 0
                    if m_i <= 0:
                        return "0 minutes"
                    if m_i % 60 == 0:
                        h = int(m_i / 60)
                        return f"{h} hour" if h == 1 else f"{h} hours"
                    return f"{m_i} minutes"

                parts = []
                for c in cleaned[:6]:
                    try:
                        ctype = str(c.get("type") or "").strip()
                        desc_s = str(c.get("description") or "").strip()
                        if ctype in ("price_above", "price_below"):
                            lvl = c.get("level")
                            direction = "above" if ctype == "price_above" else "below"
                            seg = f"price {direction} {lvl}"
                            if desc_s:
                                seg += f" ({desc_s})"
                            parts.append(seg)
                        elif ctype == "h1_volume_above":
                            thr = c.get("threshold")
                            seg = f"H1 volume above {thr}"
                            if desc_s:
                                seg += f" ({desc_s})"
                            parts.append(seg)
                        elif ctype == "scanner_pattern":
                            pat = c.get("pattern")
                            seg = f"pattern {pat}"
                            if desc_s:
                                seg += f" ({desc_s})"
                            parts.append(seg)
                        elif ctype in ("indicator_above", "indicator_below"):
                            ind = c.get("indicator")
                            thr = c.get("threshold")
                            direction = "above" if ctype == "indicator_above" else "below"
                            seg = f"{ind} {direction} {thr}"
                            if desc_s:
                                seg += f" ({desc_s})"
                            parts.append(seg)
                    except Exception:
                        continue

                monitoring = " and ".join(parts) if parts else f"{len(cleaned)} condition(s)"
                content = (
                    f"Got it boss. Monitoring: {monitoring}. "
                    f"Max sleep: {_fmt_minutes(msm)}."
                )
                record_agent_event("SIMBA_ACK", content, payload=payload, author="SIMBA")
            except Exception:
                pass

            self._log_tool("set_wake_conditions", start, f"count={len(cleaned)} max_sleep_minutes={msm}")
            return {"success": True, "count": len(cleaned), "max_sleep_minutes": msm}
        except Exception as e:
            self._log_tool("set_wake_conditions", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    def get_candles(self, timeframe: str, count: int) -> Dict[str, Any]:
        start = time.time()
        try:
            tf = str(timeframe or "").upper().strip()
            _TF_ALIASES = {"4H": "H4", "1H": "H1", "1D": "D1", "15M": "M15", "5M": "M5", "30M": "M30", "1M": "M1"}
            tf = _TF_ALIASES.get(tf, tf)
            if tf not in ("M1", "M5", "M15", "M30", "H1", "H4", "D1"):
                self._log_fail("get_candles", start, "unsupported timeframe")
                return {"success": False, "reason": f"unsupported timeframe '{tf}'. Use: M1, M5, M15, H1, H4, D1"}

            try:
                c = int(count)
            except Exception:
                c = 0
            if c <= 0:
                self._log_fail("get_candles", start, "count must be positive")
                return {"success": False, "reason": "count must be positive"}
            # FLO-166: H1 default 50 candles (2 days) for proper swing visibility
            if tf == "H1":
                c = max(c, 50)
            c = min(c, 100)

            dp = self._last_agent_data() or {}

            candles: Optional[List[Dict[str, Any]]] = None

            # Preferred cache source: data_package has a candles section (if present)
            try:
                cds = dp.get("candles") or {}
                if isinstance(cds, dict):
                    maybe = cds.get(tf)
                    if isinstance(maybe, list) and maybe:
                        candles = maybe
            except Exception:
                candles = None

            # H1 fallback: DataFrame cache
            if candles is None and tf == "H1":
                df = self._last_df()
                if df is None:
                    self._log_no_cache("get_candles", start, f"{tf} x {c}")
                    return self._no_cache()
                try:
                    # Expect columns: time, open, high, low, close, tick_volume/volume
                    cols = set(getattr(df, "columns", []))
                    required = {"open", "high", "low", "close"}
                    if not required.issubset(cols):
                        self._log_fail("get_candles", start, "missing cached df columns")
                        return {"success": False, "reason": "missing cached df columns"}

                    tail = df.tail(c)
                    out_list: List[Dict[str, Any]] = []
                    for _, row in tail.iterrows():
                        t = None
                        if "time" in cols:
                            try:
                                t = row["time"]
                                if hasattr(t, "isoformat"):
                                    t = t.isoformat()
                                else:
                                    t = str(t)
                            except Exception:
                                t = None

                        vol = None
                        if "tick_volume" in cols:
                            vol = row.get("tick_volume")
                        elif "volume" in cols:
                            vol = row.get("volume")

                        out_list.append(
                            {
                                "time": t,
                                "open": float(row["open"]),
                                "high": float(row["high"]),
                                "low": float(row["low"]),
                                "close": float(row["close"]),
                                "volume": float(vol) if vol is not None else 0.0,
                            }
                        )
                    candles = out_list
                except Exception:
                    self._log_fail("get_candles", start, "failed to build candles from cache")
                    return {"success": False, "reason": "failed to build candles from cache"}

            if candles is None:
                self._log_no_cache("get_candles", start, f"{tf} x {c}")
                return self._no_cache()

            candles = candles[-c:]

            # FLO-225: Enrich candles with indicator values per bar
            # Gives Floki indicator history — RSI divergences, BB squeezes, MACD patterns
            try:
                import pandas as pd
                import math
                from technical_analyzer import calculate_indicators

                def _rn(v, d):
                    """Round or None for NaN/missing values."""
                    if v is None:
                        return None
                    try:
                        f = float(v)
                        if math.isnan(f):
                            return None
                        return round(f, d)
                    except Exception:
                        return None

                _edf = pd.DataFrame(candles)
                if len(_edf) >= 14 and {"open", "high", "low", "close"}.issubset(_edf.columns):
                    _edf = calculate_indicators(_edf)
                    for _ei in range(len(candles)):
                        _er = _edf.iloc[_ei]
                        candles[_ei]["rsi"] = _rn(_er.get("rsi_14"), 1)
                        candles[_ei]["macd"] = _rn(_er.get("macd"), 2)
                        candles[_ei]["macd_signal"] = _rn(_er.get("macd_signal"), 2)
                        candles[_ei]["macd_hist"] = _rn(_er.get("macd_hist"), 2)
                        _bbu = _rn(_er.get("bb_upper"), 2)
                        _bbl = _rn(_er.get("bb_lower"), 2)
                        candles[_ei]["bb_upper"] = _bbu
                        candles[_ei]["bb_lower"] = _bbl
                        candles[_ei]["bb_mid"] = _rn(_er.get("bb_middle"), 2)
                        candles[_ei]["bb_width"] = round(_bbu - _bbl, 2) if _bbu is not None and _bbl is not None else None
                        candles[_ei]["ema9"] = _rn(_er.get("ema_9"), 2)
                        candles[_ei]["ema21"] = _rn(_er.get("ema_21"), 2)
                        candles[_ei]["ema50"] = _rn(_er.get("ema_50"), 2)
                        candles[_ei]["ema200"] = _rn(_er.get("ema_200"), 2)
            except Exception:
                pass  # Enrichment failure is non-fatal — return plain candles

            self._log_tool("get_candles", start, f"{tf} x {len(candles)}")
            return {"timeframe": tf, "candles": candles}
        except Exception as e:
            self._log_tool("get_candles", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    def get_indicators(self, timeframe: str = "") -> Dict[str, Any]:
        start = time.time()
        try:
            dp = self._last_agent_data()
            if not dp:
                self._log_no_cache("get_indicators", start)
                return self._no_cache()

            # FLO-290 commit 4: when a timeframe param is supplied, route through
            # the per-TF multi_tf_indicators dict (populated by central Brain at
            # main.py:1968, now covering M1/M5/M15/H1/H4/D1). When no param is
            # given, preserve the legacy flat-H1 path so the 5 HIGH-risk consumers
            # that read dp["indicators"] flat (agent_monitor, rex _get_data_value,
            # get_pivot_points enrichment, _get_full_mtf_signals, etc.) don't break.
            tf = (timeframe or "").strip().upper()
            if tf and tf in ("M1", "M5", "M15", "H1", "H4", "D1"):
                mtf = dp.get("multi_tf_indicators") or {}
                if isinstance(mtf, dict) and tf in mtf:
                    tf_ind = mtf[tf]
                    if isinstance(tf_ind, dict) and tf_ind:
                        self._log_tool("get_indicators", start, f"tf={tf}")
                        return {"timeframe": tf, **tf_ind}
                self._log_tool("get_indicators", start, f"tf={tf} not in multi_tf_indicators, falling back")
                # fall through to flat path

            ind = dp.get("indicators")
            if not isinstance(ind, dict) or not ind:
                self._log_no_cache("get_indicators", start)
                return self._no_cache()

            # Return a simplified, model-friendly view while preserving numeric values.
            out: Dict[str, Any] = {}

            try:
                rsi = ind.get("rsi") or {}
                out["rsi"] = self._safe_float(rsi.get("value"))
            except Exception:
                out["rsi"] = None

            try:
                macd = ind.get("macd") or {}
                out["macd"] = {
                    "value": self._safe_float(macd.get("value")),
                    "signal": self._safe_float(macd.get("signal")),
                    "histogram": self._safe_float(macd.get("histogram")),
                }
            except Exception:
                out["macd"] = {"value": None, "signal": None, "histogram": None}

            try:
                emas = ind.get("emas") or {}
                out["ema50"] = self._safe_float(emas.get("ema50"))
                out["ema200"] = self._safe_float(emas.get("ema200"))
            except Exception:
                out["ema50"] = None
                out["ema200"] = None

            try:
                atr = ind.get("atr") or {}
                out["atr"] = self._safe_float(atr.get("value"))
            except Exception:
                out["atr"] = None

            try:
                adx = ind.get("adx") or {}
                out["adx"] = {
                    "value": self._safe_float(adx.get("value")),
                    "plus_di": self._safe_float(adx.get("plus_di")),
                    "minus_di": self._safe_float(adx.get("minus_di")),
                }
            except Exception:
                out["adx"] = {"value": None, "plus_di": None, "minus_di": None}

            try:
                bb = ind.get("bollinger") or {}
                out["bollinger"] = {
                    "upper": self._safe_float(bb.get("upper")),
                    "middle": self._safe_float(bb.get("middle")),
                    "lower": self._safe_float(bb.get("lower")),
                    "position_pct": self._safe_float(bb.get("position_pct")),
                }
            except Exception:
                out["bollinger"] = {"upper": None, "middle": None, "lower": None, "position_pct": None}

            # FLO-164 Fix 1: 5-bar trend enrichment from H1 candle history
            try:
                import numpy as np
                dp_candles = dp.get("candles", {})
                h1_raw = dp_candles.get("H1") if isinstance(dp_candles, dict) else None
                if isinstance(h1_raw, list) and len(h1_raw) >= 6:
                    closes = [float(c.get("close", c[4]) if isinstance(c, dict) else c[4]) for c in h1_raw[-20:]]
                    highs = [float(c.get("high", c[2]) if isinstance(c, dict) else c[2]) for c in h1_raw[-20:]]
                    lows = [float(c.get("low", c[3]) if isinstance(c, dict) else c[3]) for c in h1_raw[-20:]]

                    def _rsi(data, period=14):
                        if len(data) < period + 1:
                            return None
                        deltas = np.diff(data)
                        gains = np.where(deltas > 0, deltas, 0)
                        losses = np.where(deltas < 0, -deltas, 0)
                        ag = np.mean(gains[-period:])
                        al = np.mean(losses[-period:])
                        if al == 0:
                            return 100.0
                        return round(100 - (100 / (1 + ag / al)), 1)

                    rsi_now = _rsi(closes)
                    rsi_5ago = _rsi(closes[:-5]) if len(closes) > 19 else None

                    if rsi_now is not None and rsi_5ago is not None:
                        out["rsi_5bar_ago"] = rsi_5ago
                        diff = rsi_now - rsi_5ago
                        out["rsi_direction"] = "rising" if diff > 3 else ("falling" if diff < -3 else "flat")

                    # MACD histogram trend
                    if len(closes) >= 26:
                        def _macd_hist(data):
                            ema12 = [data[0]]
                            ema26 = [data[0]]
                            m12, m26 = 2.0/13.0, 2.0/27.0
                            for c in data[1:]:
                                ema12.append(c * m12 + ema12[-1] * (1 - m12))
                                ema26.append(c * m26 + ema26[-1] * (1 - m26))
                            macd_line = [e12 - e26 for e12, e26 in zip(ema12, ema26)]
                            signal = [macd_line[0]]
                            m9 = 2.0/10.0
                            for v in macd_line[1:]:
                                signal.append(v * m9 + signal[-1] * (1 - m9))
                            return macd_line[-1] - signal[-1]

                        hist_now = _macd_hist(closes)
                        hist_5ago = _macd_hist(closes[:-5])
                        diff_h = hist_now - hist_5ago
                        out["macd_histogram_direction"] = "rising" if diff_h > 0.5 else ("falling" if diff_h < -0.5 else "flat")

                    # ADX direction from actual 4-bar change (FLO-240: no threshold bias)
                    adx_change = self._safe_float(out.get("adx_change_4bars"))
                    if adx_change is not None:
                        out["adx_direction"] = "rising" if adx_change > 2 else ("falling" if adx_change < -2 else "steady")

                    # Bollinger width + direction
                    bb_u = self._safe_float((ind.get("bollinger") or {}).get("upper"))
                    bb_l = self._safe_float((ind.get("bollinger") or {}).get("lower"))
                    bb_m = self._safe_float((ind.get("bollinger") or {}).get("middle"))
                    if bb_u and bb_l and bb_m and bb_m > 0:
                        width_now = (bb_u - bb_l) / bb_m * 100
                        out["bb_width_pct"] = round(width_now, 2)
                        # Estimate 5-bar-ago width from candle range
                        if len(highs) >= 20 and len(lows) >= 20:
                            avg_range_recent = np.mean([h - l for h, l in zip(highs[-5:], lows[-5:])])
                            avg_range_prior = np.mean([h - l for h, l in zip(highs[-10:-5], lows[-10:-5])])
                            if avg_range_prior > 0:
                                out["bb_width_direction"] = "expanding" if avg_range_recent > avg_range_prior * 1.15 else ("squeezing" if avg_range_recent < avg_range_prior * 0.85 else "stable")
            except Exception:
                pass

            # FLO-221: Append multi-TF indicators (M15, H1, H4, D1)
            try:
                mtf = dp.get("multi_tf_indicators")
                if isinstance(mtf, dict) and mtf:
                    for tf_key in ["M15", "H1", "H4", "D1"]:
                        if tf_key in mtf:
                            out[tf_key] = mtf[tf_key]
            except Exception:
                pass

            self._log_tool("get_indicators", start)
            return out
        except Exception as e:
            self._log_tool("get_indicators", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    def get_sr_zones(self, timeframe: str = "") -> Dict[str, Any]:
        start = time.time()
        try:
            # FLO-262: If timeframe specified, use per-TF zones
            tf = (timeframe or "").strip().upper()
            if tf and tf in ("D1", "H4", "H1", "M15", "M5", "M1"):
                per_tf = getattr(self._bot, '_last_sr_zones_per_tf', None)
                if per_tf and isinstance(per_tf, dict) and tf in per_tf:
                    tf_zones = per_tf[tf]
                    zones = []
                    for z in tf_zones:
                        zones.append({
                            "price": round(z.midpoint, 2),
                            "zone_type": z.zone_type,
                            "touches": z.touches,
                            "timeframe": z.timeframe,
                            "confluence": z.confluence if z.confluence else [],
                            "strength": z.strength,
                            "is_confluence": len(z.confluence) > 1,
                            "volume": int(getattr(z, "volume", 0)),            # FLO-312
                            "volume_bucket": getattr(z, "volume_bucket", "—"),   # FLO-312
                        })
                    _h = sum(1 for z in zones if z.get("volume_bucket") == "HIGH")
                    _l = sum(1 for z in zones if z.get("volume_bucket") == "LOW")
                    self._log_tool("get_sr_zones", start,
                                   f"tf={tf} zones={len(zones)} vol_H/L={_h}/{_l}")
                    # Fall through to enrichment below
                    # (skip the merged-zones path)
                else:
                    self._log_tool("get_sr_zones", start, f"tf={tf} per-TF data not available, using merged")
                    tf = ""  # fall through to merged path

            if not tf:
                dp = self._last_agent_data()
                if not dp:
                    self._log_no_cache("get_sr_zones", start)
                    return self._no_cache()

                sr = dp.get("sr_zones") or dp.get("support_resistance")
                if isinstance(sr, dict) and "zones" in sr:
                    zones = sr.get("zones")
                else:
                    zones = sr

            if not isinstance(zones, list) or not zones:
                self._log_no_cache("get_sr_zones", start)
                return self._no_cache()

            # FLO-111: Filter to 8 most relevant zones (4 above + 4 below price)
            raw_count = len(zones)
            try:
                cp = dp.get("current_price") or {}
                price = self._safe_float(cp.get("mid")) or self._safe_float(cp.get("bid"))
                if price:
                    above = []
                    below = []
                    for z in zones:
                        zp = z.get("price") if isinstance(z, dict) else getattr(z, "midpoint", None)
                        if zp is None:
                            zp = z.get("midpoint", 0) if isinstance(z, dict) else 0
                        if zp > price:
                            above.append(z)
                        else:
                            below.append(z)
                    above.sort(key=lambda z: abs((z.get("price") or z.get("midpoint", 0)) - price))
                    below.sort(key=lambda z: abs((z.get("price") or z.get("midpoint", 0)) - price))
                    # FLO-299 #16: capture pre-cap totals so Floki can see the
                    # full S/R picture (how many zones existed above/below).
                    _total_above = len(above)
                    _total_below = len(below)
                    zones = above[:4] + below[:4]
                    try:
                        self._last_sr_meta = {
                            "total_zones": raw_count,
                            "total_above": _total_above,
                            "total_below": _total_below,
                            "showing_above": min(4, _total_above),
                            "showing_below": min(4, _total_below),
                        }
                    except Exception:
                        pass
            except Exception:
                pass

            # FLO-240: Cross-reference with pivot points, confluence zones first
            try:
                _bs_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "bot_state.json")
                with open(_bs_path, "r", encoding="utf-8") as _bsf:
                    _bs_sr = json.load(_bsf)
                _pivots = {}
                for _layer in ("daily", "weekly", "monthly"):
                    _cl = _bs_sr.get("pivot_points", {}).get(_layer, {}).get("classic", {})
                    for _pk, _pv in _cl.items():
                        if _pv:
                            _pivots[f"{_layer}_{_pk}"] = float(_pv)
                _with_conf = []
                _without_conf = []
                for z in zones:
                    zp = float(z.get("price") or z.get("midpoint", 0) or 0)
                    if not zp:
                        _without_conf.append(z)
                        continue
                    confl = []
                    for _pk, _pv in _pivots.items():
                        if abs(zp - _pv) < 10:
                            confl.append(f"{_pk} ({abs(zp - _pv):.1f})")
                    if confl:
                        z["pivot_confluence"] = confl
                        _with_conf.append(z)
                    else:
                        _without_conf.append(z)
                if len(_with_conf) >= 3:
                    zones = _with_conf
                else:
                    zones = _with_conf + _without_conf
            except Exception:
                pass

            # FLO-244: Label zones with role + direction-aware test_type for nearby levels
            try:
                _cp_role = self._safe_float((dp.get("current_price") or {}).get("mid")) or self._safe_float((dp.get("current_price") or {}).get("bid"))
                if _cp_role:
                    # Determine price direction from bot_state
                    _price_dir = "FLAT"
                    try:
                        _bs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "bot_state.json")
                        with open(_bs_dir, "r", encoding="utf-8") as _fd:
                            _pct = json.load(_fd).get("price_daily_change_pct", 0) or 0
                        _price_dir = "FALLING" if _pct < -0.1 else ("RISING" if _pct > 0.1 else "FLAT")
                    except Exception:
                        pass

                    for z in zones:
                        _zp = float(z.get("price") or z.get("midpoint", 0) or 0)
                        _zt = str(z.get("zone_type", "")).upper()
                        _dist = round(abs(_cp_role - _zp), 1)

                        # Base role from position — overwrite zone_type so Floki says SUPPORT/RESISTANCE not FLIP
                        if _zp < _cp_role:
                            z["role"] = "SUPPORT"
                            if _zt == "FLIP":
                                z["flip_phase"] = "resistance \u2192 support"
                            z["zone_type"] = "SUPPORT"
                        elif _zp > _cp_role:
                            z["role"] = "RESISTANCE"
                            if _zt == "FLIP":
                                z["flip_phase"] = "support \u2192 resistance"
                            z["zone_type"] = "RESISTANCE"
                        else:
                            z["role"] = "AT_PRICE"
                            z["zone_type"] = "AT_PRICE"
                        z["distance_pips"] = _dist

                        # Direction-aware test type for nearby zones (<5 pips)
                        if _dist < 5:
                            if _price_dir == "FALLING":
                                z["test_type"] = "SUPPORT_TEST"
                                z["test_note"] = f"Price falling toward {_zp:.1f} \u2014 testing as support"
                            elif _price_dir == "RISING":
                                z["test_type"] = "RESISTANCE_TEST"
                                z["test_note"] = f"Price rising toward {_zp:.1f} \u2014 testing as resistance"
                            else:
                                z["test_type"] = "CONSOLIDATING"
                                z["test_note"] = f"Price flat near {_zp:.1f} \u2014 consolidating at level"
            except Exception:
                pass

            self._log_tool("get_sr_zones", start, f"zones={len(zones)} (raw={raw_count})")
            # FLO-299 #16: include truncation metadata so Floki knows how many
            # zones existed before the 4-above/4-below cap.
            _meta = getattr(self, "_last_sr_meta", None) or {}
            _resp = {"zones": zones, "total_zones": raw_count, "showing": len(zones)}
            if _meta:
                _resp["split"] = {
                    "showing_above": _meta.get("showing_above"),
                    "total_above": _meta.get("total_above"),
                    "showing_below": _meta.get("showing_below"),
                    "total_below": _meta.get("total_below"),
                }
            return _resp
        except Exception as e:
            self._log_tool("get_sr_zones", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    def get_volume_profile(
        self,
        window_hours: float = 1.0,
        bucket_size_points: float = 1.0,
        top_n_nodes: int = 5,
    ) -> Dict[str, Any]:
        """FLO-319: Volume profile aggregated from M1/M5/M15 tick_volume.

        Returns POC, value area, top HVNs, low-volume gaps, current price
        context. Cached with 60s TTL so multiple calls in a single Brain
        cycle don't recompute.
        """
        start = time.time()
        try:
            cache_key = (
                round(float(window_hours), 2),
                round(float(bucket_size_points), 2),
                int(top_n_nodes),
            )
            # 60s TTL cache — covers one Brain cycle + slack for slow cycles
            cache = getattr(self._bot, "_volume_profile_cache", None)
            if not isinstance(cache, dict):
                cache = {}
            entry = cache.get(cache_key)
            if entry and (time.time() - entry["t"]) < 60:
                self._log_tool(
                    "get_volume_profile", start,
                    f"cached | hours={window_hours} bucket={bucket_size_points}"
                )
                return {"success": True, "profile": entry["v"], "cached": True}

            from volume_profile import compute_volume_profile
            profile = compute_volume_profile(
                symbol="XAUUSD",
                window_hours=float(window_hours),
                bucket_size_points=float(bucket_size_points),
                top_n_nodes=int(top_n_nodes),
            )
            if profile is None:
                self._log_tool("get_volume_profile", start, "compute_failed")
                return {"success": False, "reason": "compute_failed (no bars / MT5 error)"}

            cache[cache_key] = {"t": time.time(), "v": profile}
            self._bot._volume_profile_cache = cache
            poc_p = profile["poc"]["price"]
            self._log_tool(
                "get_volume_profile", start,
                f"hours={window_hours} bars={profile['bars_used']} poc={poc_p} "
                f"nodes={len(profile['top_nodes'])} gaps={len(profile['low_volume_gaps'])}"
            )
            return {"success": True, "profile": profile, "cached": False}
        except Exception as e:
            self._log_tool("get_volume_profile", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    def get_tick_pressure(
        self,
        window_seconds: int = 300,
        recent_seconds: int = 30,
    ) -> Dict[str, Any]:
        """FLO-320: Directional tick-pressure proxy (NOT true order flow).

        Capital Point publishes a quote stream — no buy/sell flags, no
        trade volume. This tool computes the classic mid-price tick rule
        over a rolling window as a directional-pressure proxy. The
        response includes a 'note' field reminding Floki this is a proxy.

        Cached 20s (ticks change fast — tighter TTL than volume profile).
        """
        start = time.time()
        try:
            cache_key = (int(window_seconds), int(recent_seconds))
            cache = getattr(self._bot, "_tick_pressure_cache", None)
            if not isinstance(cache, dict):
                cache = {}
            entry = cache.get(cache_key)
            if entry and (time.time() - entry["t"]) < 20:
                self._log_tool(
                    "get_tick_pressure", start,
                    f"cached | window={window_seconds}s"
                )
                return {"success": True, "pressure": entry["v"], "cached": True}

            from tick_pressure import compute_tick_pressure
            pressure = compute_tick_pressure(
                symbol="XAUUSD",
                window_seconds=int(window_seconds),
                recent_seconds=int(recent_seconds),
            )
            if pressure is None:
                self._log_tool("get_tick_pressure", start, "compute_failed")
                return {"success": False, "reason": "compute_failed (no ticks / MT5 error)"}

            cache[cache_key] = {"t": time.time(), "v": pressure}
            self._bot._tick_pressure_cache = cache
            self._log_tool(
                "get_tick_pressure", start,
                f"window={window_seconds}s ticks={pressure['total_ticks']} "
                f"uptick_ratio={pressure['uptick_ratio']} recent={pressure['recent_pressure']}"
            )
            return {"success": True, "pressure": pressure, "cached": False}
        except Exception as e:
            self._log_tool("get_tick_pressure", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    def get_session_context(
        self,
        window_sessions: int = 20,
    ) -> Dict[str, Any]:
        """FLO-332: Session-specific market context.

        Answers "how does the current session compare to normal for this session?"
        by comparing current volume + range against the last N same sessions,
        normalized to "typical at the same elapsed minutes into the session".
        Cached 60s.
        """
        start = time.time()
        try:
            cache_key = int(window_sessions)
            cache = getattr(self._bot, "_session_context_cache", None)
            if not isinstance(cache, dict):
                cache = {}
            entry = cache.get(cache_key)
            if entry and (time.time() - entry["t"]) < 60:
                self._log_tool(
                    "get_session_context", start,
                    f"cached | window={window_sessions}"
                )
                return {"success": True, "context": entry["v"], "cached": True}

            from session_context import compute_session_context
            ctx = compute_session_context(
                symbol="XAUUSD",
                window_sessions=int(window_sessions),
            )
            if ctx is None:
                self._log_tool("get_session_context", start, "compute_failed")
                return {"success": False, "reason": "compute_failed (no bars / MT5 error)"}

            cache[cache_key] = {"t": time.time(), "v": ctx}
            self._bot._session_context_cache = cache
            self._log_tool(
                "get_session_context", start,
                f"session={ctx['session']} elapsed={ctx['session_elapsed_min']}m "
                f"vol={ctx['volume']['classification']} range={ctx['range_pts']['classification']} "
                f"overall={ctx['overall_classification']} n_hist={ctx['n_historical_sessions']}"
            )
            return {"success": True, "context": ctx, "cached": False}
        except Exception as e:
            self._log_tool("get_session_context", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    def get_fibonacci_levels(self, timeframe: str = "") -> Dict[str, Any]:
        """FLO-290 commit 4: fixes long-standing dead-code path.

        Before: the per-TF branch required `levels` to be a dict, but
        _compute_fibonacci_from_h1 returns `levels` as a LIST of {pct, price}
        dicts. Result: tool was returning no-data since deployment. Also, no
        per-TF Fib data was ever populated upstream — the H1/H4/D1 branch
        never matched.

        Now: upstream agent_data_builder populates fib["M1"]/fib["M5"]/.../fib["D1"]
        via _compute_fibonacci_from_candles. Tool returns per-TF slice (or all
        TFs if no timeframe param given). levels passed through as list — tool
        no longer discriminates against the real shape.
        """
        start = time.time()
        try:
            dp = self._last_agent_data()
            if not dp:
                self._log_no_cache("get_fibonacci_levels", start)
                return self._no_cache()

            fib = dp.get("fibonacci") or dp.get("fib")
            if not isinstance(fib, dict) or not fib:
                self._log_no_cache("get_fibonacci_levels", start)
                return self._no_cache()

            SUPPORTED = ("M1", "M5", "M15", "H1", "H4", "D1")

            def _pack(tf_key, v):
                if not isinstance(v, dict):
                    return None
                return {
                    "swing_high": v.get("swing_high"),
                    "swing_low": v.get("swing_low"),
                    "direction": v.get("direction"),
                    "levels": v.get("levels"),  # list of {pct, price} — no longer dropped
                }

            tf_arg = (timeframe or "").strip().upper()
            if tf_arg and tf_arg in SUPPORTED:
                packed = _pack(tf_arg, fib.get(tf_arg))
                if packed and packed["levels"]:
                    self._log_tool("get_fibonacci_levels", start, f"tf={tf_arg}")
                    return {tf_arg: packed}
                self._log_no_cache("get_fibonacci_levels", start, f"tf={tf_arg} no data")
                return self._no_cache()

            # No param: return every populated TF
            out = {}
            for tf in SUPPORTED:
                packed = _pack(tf, fib.get(tf))
                if packed and packed["levels"]:
                    out[tf] = packed
            if out:
                self._log_tool("get_fibonacci_levels", start, f"tfs={','.join(out.keys())}")
                return out

            # Last-resort flat-H1 fallback (pre-FLO-290 data shape)
            flat_levels = fib.get("levels")
            if flat_levels:
                self._log_tool("get_fibonacci_levels", start, "tfs=H1 (flat fallback)")
                return {
                    "H1": {
                        "swing_high": fib.get("swing_high"),
                        "swing_low": fib.get("swing_low"),
                        "direction": fib.get("direction"),
                        "levels": flat_levels,
                    }
                }

            self._log_no_cache("get_fibonacci_levels", start)
            return self._no_cache()
        except Exception as e:
            self._log_tool("get_fibonacci_levels", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    def get_pivot_points(self) -> Dict[str, Any]:
        """FLO-223: Return Classic + Fibonacci Pivot Points from previous D1 candle."""
        start = time.time()
        try:
            dp = self._last_agent_data()
            if not dp:
                self._log_no_cache("get_pivot_points", start)
                return self._no_cache()

            pp = dp.get("pivot_points")
            if not isinstance(pp, dict) or not pp:
                self._log_no_cache("get_pivot_points", start)
                return self._no_cache()

            self._log_tool("get_pivot_points", start, f"PP={pp.get('classic', {}).get('PP', '?')}")
            return pp
        except Exception as e:
            self._log_tool("get_pivot_points", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    # ---------------------------------------------------------------------
    # Context tools (cache-only)
    # ---------------------------------------------------------------------

    def get_headlines(self) -> Dict[str, Any]:
        start = time.time()
        try:
            dp = self._last_agent_data()
            if not dp:
                self._log_no_cache("get_headlines", start)
                return self._no_cache()

            news = dp.get("headlines") or dp.get("news") or dp.get("news_headlines")
            if isinstance(news, dict) and "headlines" in news:
                headlines = news.get("headlines")
            else:
                headlines = news

            if not isinstance(headlines, list):
                self._log_no_cache("get_headlines", start)
                return self._no_cache()

            # FLO-299 #17: surface total headline count so Floki knows how many
            # he's not seeing.
            out = {
                "headlines": headlines[:10],
                "count": min(len(headlines), 10),
                "total_headlines": len(headlines),
                "showing": min(len(headlines), 10),
            }
            self._log_tool("get_headlines", start, f"count={out['count']} total={out['total_headlines']}")
            return out
        except Exception as e:
            self._log_tool("get_headlines", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    def get_macro(self) -> Dict[str, Any]:
        start = time.time()
        try:
            dp = self._last_agent_data()
            if not dp:
                self._log_no_cache("get_macro", start)
                return self._no_cache()

            macro = dp.get("macro")
            if not isinstance(macro, dict) or not macro:
                self._log_no_cache("get_macro", start)
                return self._no_cache()

            self._log_tool("get_macro", start)
            return macro
        except Exception as e:
            self._log_tool("get_macro", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    def get_calendar(self) -> Dict[str, Any]:
        start = time.time()
        try:
            dp = self._last_agent_data()
            if not dp:
                self._log_no_cache("get_calendar", start)
                return self._no_cache()

            cal = dp.get("calendar") or dp.get("economic_calendar")
            if not isinstance(cal, dict) or not cal:
                self._log_no_cache("get_calendar", start)
                return self._no_cache()

            self._log_tool("get_calendar", start)
            return cal
        except Exception as e:
            self._log_tool("get_calendar", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    def get_ml_prediction(self) -> Dict[str, Any]:
        start = time.time()
        try:
            dp = self._last_agent_data()
            if not dp:
                self._log_no_cache("get_ml_prediction", start)
                return self._no_cache()

            ml = dp.get("ml") or dp.get("ml_prediction") or dp.get("ml_predictions")
            if not isinstance(ml, dict) or not ml:
                self._log_no_cache("get_ml_prediction", start)
                return self._no_cache()

            self._log_tool("get_ml_prediction", start)
            return ml
        except Exception as e:
            self._log_tool("get_ml_prediction", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    # ---------------------------------------------------------------------
    # Market context (correlated instruments from MT5)
    # ---------------------------------------------------------------------

    def get_market_context(self) -> Dict[str, Any]:
        """Read correlated MT5 instruments for broader market picture."""
        start = time.time()
        try:
            from market_context_fetcher import fetch_market_context

            result = fetch_market_context()
            if not result:
                self._log_tool("get_market_context", start, "no_data")
                return {"success": False, "reason": "no_data"}

            # Enrich with volume ratio from agent data (not available in fetcher)
            try:
                dp = self._last_agent_data()
                vol = (dp.get("indicators") or {}).get("volume") if dp else None
                if isinstance(result.get("session"), dict) and isinstance(vol, dict):
                    result["session"]["volume_ratio"] = vol.get("tick_volume_ratio")
            except Exception:
                pass

            n_live = sum(1 for cat in result.values() if isinstance(cat, dict) for v in cat.values() if isinstance(v, dict) and v.get("bid"))
            self._log_tool("get_market_context", start, f"live={n_live}")
            return result
        except Exception as e:
            self._log_tool("get_market_context", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    # ---------------------------------------------------------------------
    # Portfolio tools (execution layer is allowed)
    # ---------------------------------------------------------------------

    def get_open_positions(self) -> Dict[str, Any]:
        start = time.time()
        try:
            positions = []
            try:
                positions = self._executor.get_open_positions() or []
            except Exception:
                positions = []

            out_positions = []
            for p in positions:
                try:
                    _comment = str(getattr(p, "comment", "") or "")
                    out_positions.append(
                        {
                            "ticket": int(getattr(p, "ticket", 0)),
                            "direction": str(getattr(p, "direction", "")),
                            "entry": float(getattr(p, "open_price", 0.0)),
                            "sl": float(getattr(p, "sl", 0.0)),
                            "tp": float(getattr(p, "tp", 0.0)),
                            "current_pnl": float(getattr(p, "profit", 0.0)),
                            "phase": "OPEN",
                            # FLO-361 — surface MT5 comment so Floki can
                            # distinguish Snow-managed positions
                            # (comment starts with "snow:") from his own.
                            "comment": _comment,
                            "managed_by": (
                                "snow" if _comment.startswith("snow:")
                                else "floki"
                            ),
                        }
                    )
                except Exception:
                    continue

            self._log_tool("get_open_positions", start, f"count={len(out_positions)}")
            result = {"positions": out_positions, "count": len(out_positions)}

            # FLO-292: Detect duplicate positions (same direction/entry/SL/TP) —
            # likely artifact of EA/direct-API race condition that FLO-291 guards against.
            dup_groups: Dict[tuple, List[int]] = {}
            for p in out_positions:
                key = (
                    p["direction"].upper(),
                    round(p["entry"], 1),
                    round(p.get("sl", 0), 0),
                    round(p.get("tp", 0), 0),
                )
                dup_groups.setdefault(key, []).append(p["ticket"])
            duplicates = [tks for tks in dup_groups.values() if len(tks) >= 2]
            if duplicates:
                result["duplicate_suspected"] = duplicates
                result["duplicate_hint"] = (
                    "Multiple positions share direction/entry/SL/TP — possible "
                    "race-condition duplicate. Consider closing one and investigating."
                )
            return result
        except Exception as e:
            self._log_tool("get_open_positions", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    def get_account_info(self) -> Dict[str, Any]:
        start = time.time()
        try:
            info = self._executor.get_account_info()
            if not isinstance(info, dict) or not info:
                return {"success": False, "reason": "account info unavailable"}

            out = {
                "balance": self._safe_float(info.get("balance")),
                "equity": self._safe_float(info.get("equity")),
                "margin_used": self._safe_float(info.get("margin")),
                "leverage": info.get("leverage"),
            }
            self._log_tool("get_account_info", start)
            return out
        except Exception as e:
            self._log_tool("get_account_info", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    def get_trade_history(self, days: int) -> Dict[str, Any]:
        start = time.time()
        try:
            try:
                d = int(days)
            except Exception:
                d = 1
            d = max(1, min(d, 30))

            # Prefer db_writer if available
            trades = []
            summary = {"wins": 0, "losses": 0, "pnl": 0.0}
            try:
                from db_writer import get_recent_agent_decisions  # noqa: F401
                # No dedicated helper found here for trade history; fall back to MT5 deal history helper if exposed.
            except Exception:
                pass

            # Use existing executor helper if available
            try:
                from executor import get_recent_closed_deals

                deals = get_recent_closed_deals(hours=d * 24) or []
                for deal in deals:
                    try:
                        profit = float(deal.get("profit", 0.0) or 0.0)
                        trades.append(
                            {
                                "ticket": int(deal.get("position_id", 0) or 0),
                                "direction": deal.get("direction"),
                                "profit": profit,
                                "close_reason": deal.get("reason"),
                                "close_time": str(deal.get("close_time")),
                            }
                        )
                        summary["pnl"] += profit
                        if profit > 0:
                            summary["wins"] += 1
                        elif profit < 0:
                            summary["losses"] += 1
                    except Exception:
                        continue
            except Exception:
                # No history available is not fatal
                pass

            self._log_tool("get_trade_history", start, f"days={d} trades={len(trades)}")
            return {"trades": trades, "summary": summary}
        except Exception as e:
            self._log_tool("get_trade_history", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    # ---------------------------------------------------------------------
    # Action tools (execution allowed; safety enforced here)
    # ---------------------------------------------------------------------

    def execute_trade(
        self,
        direction: str,
        sl: float,
        tp: float,
        agent_confidence: Optional[float] = None,
    ) -> Dict[str, Any]:
        start = time.time()
        try:
            dp = self._last_agent_data()
            if not dp:
                return self._no_cache()

            price = self._extract_price_from_cache(dp)
            if not price:
                return self._no_cache()

            try:
                import config

                max_spread = float(getattr(config, "MAX_SPREAD_PIPS", 5.0))
            except Exception:
                max_spread = 5.0

            try:
                spread_pips = float(price.get("spread") or 0.0)
                if spread_pips > max_spread:
                    self._log_tool("execute_trade", start, f"{str(direction).upper()} | REJECTED | spread {spread_pips:.1f} > max {max_spread:.1f}")
                    return {
                        "success": False,
                        "reason": f"spread too high: {spread_pips:.1f} pips > max {max_spread:.1f} pips",
                    }
            except Exception:
                pass

            m5_warning = None

            # M5 reversal check (warn only; never block)
            try:
                from momentum_detector import check_m5_reversal

                m5_check = check_m5_reversal(str(direction or ""))
                if isinstance(m5_check, dict) and m5_check.get("reversal_detected"):
                    strength = str(m5_check.get("reversal_strength") or "").lower()
                    if strength == "strong":
                        m5_warning = "M5 ALERT: strong counter-movement detected"
                        desc = str(m5_check.get("description") or "M5 reversal")
                        log.warning(f"AGENT_TOOL | {m5_warning} | {desc}")
                    elif strength == "moderate":
                        m5_warning = "M5 NOTE: moderate counter-movement"
                        desc = str(m5_check.get("description") or "M5 reversal")
                        log.warning(f"AGENT_TOOL | {m5_warning} | {desc}")
            except Exception:
                # Fail-open: reversal check must never block execution due to tool errors
                pass

            dir_s = str(direction or "").upper().strip()
            if dir_s not in ("BUY", "SELL"):
                return {"success": False, "reason": "invalid direction"}

            sl_f = self._safe_float(sl)
            tp_f = self._safe_float(tp)
            if sl_f is None or tp_f is None:
                return {"success": False, "reason": "invalid sl/tp"}

            # Compute entry reference from cached bid/ask (analysis price). Execution will get real tick.
            entry_ref = float(price["ask"] if dir_s == "BUY" else price["bid"])

            sl_pips = self._sl_pips_from_prices(entry_ref, sl_f)
            if sl_pips is None:
                self._log_tool("execute_trade", start, f"{dir_s} | REJECTED | could not compute sl pips")
                return {"success": False, "reason": "could not compute sl pips"}

            # Safety checks (market open, MT5 connected, opposing positions)
            acct = self._executor.get_account_info() or {}
            balance = self._safe_float(acct.get("balance"))
            if balance is None:
                self._log_tool("execute_trade", start, f"{dir_s} | REJECTED | account balance unavailable")
                return {"success": False, "reason": "account balance unavailable"}

            open_positions_list = None
            try:
                open_positions_list = self._executor.get_open_positions() or []
            except Exception:
                log.warning("EXECUTE_TRADE | Position fetch failed — opposing guard will block")
                open_positions_list = None

            is_safe, reasons = self._safety.is_safe_to_trade(
                account_balance=float(balance),
                open_positions=len(open_positions_list),
                mt5_connected=bool(self._executor.is_connected()) if hasattr(self._executor, "is_connected") else True,
                has_high_impact_news=False,
                trade_direction=dir_s,
                open_positions_list=open_positions_list,
            )
            if not is_safe:
                self._log_tool("execute_trade", start, f"{dir_s} | REJECTED | safety: {'; '.join(reasons[:3])}")
                return {"success": False, "reason": "; ".join(reasons[:3])}

            # Risk sizing (max 2% enforced by config via caller; we use configured RISK_PER_TRADE)
            try:
                import config

                risk_pct = float(getattr(config, "RISK_PER_TRADE", 2.0))
            except Exception:
                risk_pct = 2.0

            pos = self._risk.calculate_position_size(
                account_balance=float(balance),
                risk_percent=risk_pct,
                stop_loss_pips=float(sl_pips),
            )

            # FLO-263: Cancel all pending orders before market execution (OCO safety)
            try:
                _pending = self._executor.get_pending_orders()
                if _pending:
                    _cancelled = self._executor.cancel_all_pending()
                    log.info(f"PENDING_ORDER | MARKET_OVERRIDE | execute_trade called → cancelled {_cancelled.get('cancelled', 0)} pending orders")
            except Exception:
                pass

            # Execute
            try:
                comment = f"Agent-{dir_s}"
                res = self._executor.execute_trade(
                    direction=dir_s,
                    lot_size=float(pos.lot_size),
                    stop_loss=float(sl_f),
                    take_profit=float(tp_f),
                    comment=comment,
                    confidence=None,
                    scenario="agent_tool",
                    risk_amount=float(pos.risk_amount),
                    risk_percent=float(risk_pct),
                )
            except Exception as e_exec:
                self._log_tool("execute_trade", start, f"{dir_s} | error={e_exec}")
                return {"success": False, "reason": "execution error"}

            if not getattr(res, "success", False):
                reason = getattr(res, "error_message", None) or "execution failed"
                self._log_tool("execute_trade", start, f"{dir_s} | success=false | {reason}")
                # FLO-299 #18: removed "Consider place_pending_order" suggestion —
                # error fact only; Floki decides the remedy.
                return {
                    "success": False,
                    "reason": str(reason),
                }

            fill_price = self._safe_float(getattr(res, "price", None))
            ticket = getattr(res, "ticket", None)

            # FLO-114: Guard against phantom trades — ticket must be a real positive int
            if not ticket or (isinstance(ticket, (int, float)) and int(ticket) <= 0):
                reason = getattr(res, "error_message", None) or "ticket_not_resolved"
                self._log_tool("execute_trade", start, f"{dir_s} | REJECTED | ticket={ticket} ({reason})")
                # FLO-299 #18: removed "Consider place_pending_order" suggestion —
                # error fact only; Floki decides the remedy.
                return {
                    "success": False,
                    "reason": str(reason),
                }

            # FLO-338 C.1: register the trade in history.db at the nearest point to MT5
            # success. Phase 1.5 audit proved 15/20 ghosts bypassed main.py:4911 (FLO-103)
            # because agent_result never reached it; writing here closes that gap.
            # record_trade_open uses INSERT OR IGNORE on ticket UNIQUE (db_writer.py:781),
            # so the belt-and-suspenders main.py:4911 call is a safe no-op. Ghost 1580068886
            # (2026-04-08) is the canonical symptom fixed here.
            try:
                import config as _cfg_c1
                _c1_on = bool(getattr(_cfg_c1, "GHOST_GUARDS_ENABLED", True))
            except Exception:
                _c1_on = True
            if _c1_on:
                try:
                    from db_writer import record_trade_open
                    record_trade_open(
                        ticket=int(ticket),
                        direction=dir_s,
                        volume=float(pos.lot_size),
                        open_price=float(fill_price) if fill_price else float(entry_ref),
                        sl=float(sl_f),
                        tp=float(tp_f),
                        comment="floki_agent",
                        decision_source="floki_agent",
                    )
                    log.info(f"FLOKI | record_trade_open(C.1) → ticket={ticket} {dir_s} @ {fill_price}")
                except Exception as e_c1:
                    log.error(f"FLOKI | record_trade_open(C.1) FAILED: ticket={ticket} err={e_c1}")
                    try:
                        from alerts import alert_error
                        alert_error(
                            "Ghost Guard C.1 Failed",
                            f"record_trade_open at agent_tools failed for ticket #{ticket} ({dir_s}): {e_c1}. "
                            f"Fallback: main.py:4911 (C.2) will retry.",
                            severity="warning",
                        )
                    except Exception:
                        pass

            # FLO-63: Save trade conditions snapshot at open time
            if ticket is not None:
                try:
                    from trade_lessons import save_trade_conditions
                    indicators = self.get_indicators() if dp else {}
                    # Bug G: Luna prescriptive fields removed from schema.
                    # Persist observational metadata only (patterns) so the
                    # lessons/trade_conditions index retains useful signal
                    # without prescription leakage.
                    luna_ctx = {}
                    try:
                        from luna_analyst import load_luna_brief
                        lb = load_luna_brief()
                        if lb:
                            _pats = lb.get("patterns_detected") or []
                            luna_ctx = {"luna_patterns": list(_pats)[:3]}
                    except Exception:
                        pass

                    utc_hour = None
                    try:
                        utc_hour = datetime.utcnow().hour
                    except Exception:
                        pass

                    rex_agreed = None
                    try:
                        hist = getattr(self, "_rex_debate_history", [])
                        if hist:
                            rex_agreed = hist[-1].get("agree")
                    except Exception:
                        pass

                    conds = {
                        "rsi_h1": self._safe_float((indicators.get("rsi") or {}).get("value") if isinstance(indicators.get("rsi"), dict) else indicators.get("rsi")),
                        "macd_h1": self._safe_float((indicators.get("macd") or {}).get("value") if isinstance(indicators.get("macd"), dict) else indicators.get("macd")),
                        "adx_h1": self._safe_float((indicators.get("adx") or {}).get("value") if isinstance(indicators.get("adx"), dict) else indicators.get("adx")),
                        "atr_h1": self._safe_float((indicators.get("atr") or {}).get("value") if isinstance(indicators.get("atr"), dict) else indicators.get("atr")),
                        "ema50_distance_pct": None,
                        "volume_h1": self._safe_float(indicators.get("volume")),
                        "session": self._infer_session_from_utc_hour(utc_hour),
                        "utc_hour": utc_hour,
                        "confidence": self._safe_float(agent_confidence),
                        "rex_agreed": rex_agreed,
                    }
                    # EMA50 distance %
                    try:
                        ema50 = self._safe_float(indicators.get("ema50"))
                        if ema50 and fill_price:
                            conds["ema50_distance_pct"] = round(((fill_price - ema50) / ema50) * 100, 2)
                    except Exception:
                        pass
                    conds.update(luna_ctx)

                    # FLO-177: snapshot market regime at trade open
                    try:
                        _bs_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "bot_state.json")
                        if os.path.exists(_bs_path):
                            with open(_bs_path, "r", encoding="utf-8") as _bsf:
                                _bs = json.load(_bsf)
                            _mr = _bs.get("market_regime") or {}
                            if isinstance(_mr, dict) and _mr.get("regime"):
                                conds["regime"] = _mr["regime"]
                    except Exception:
                        pass

                    # FLO-137: snapshot active thesis at trade open
                    try:
                        _thesis_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "active_thesis.json")
                        if os.path.exists(_thesis_path):
                            with open(_thesis_path, "r", encoding="utf-8") as _tf:
                                _thesis = json.load(_tf)
                            conds["thesis_at_open"] = {
                                "direction_bias": _thesis.get("direction_bias"),
                                "key_levels": _thesis.get("key_levels"),
                                "conditions": _thesis.get("conditions"),
                                "decision": _thesis.get("decision"),
                                "confidence": _thesis.get("confidence"),
                            }
                    except Exception:
                        pass

                    # FLO-137: snapshot Rex debate reasoning at trade open
                    try:
                        hist = getattr(self, "_rex_debate_history", [])
                        if hist:
                            last_rex = hist[-1]
                            conds["rex_at_open"] = {
                                "agree": last_rex.get("agree"),
                                "reasoning": (last_rex.get("rex") or "")[:2000],
                            }
                    except Exception:
                        pass

                    save_trade_conditions(ticket, dir_s, conds)
                except Exception:
                    pass

            try:
                last_ts = getattr(self, "_rex_debate_last_ts", None)
                turns = int(getattr(self, "_rex_debate_turns", 0) or 0)
                if last_ts is not None and (time.time() - float(last_ts)) <= 300 and turns > 0:
                    log.info(f"DEBATE | complete | {turns} turns | outcome=EXECUTE")
            except Exception:
                pass

            self._log_tool(
                "execute_trade",
                start,
                f"{dir_s} @ {fill_price} | ticket={ticket} | success",
            )

            return {
                "success": True,
                "ticket": int(ticket) if ticket is not None else None,
                "fill_price": fill_price,
                "volume": float(pos.lot_size),
                "direction": dir_s,
                "sl": float(sl_f),
                "tp": float(tp_f),
                "warning": m5_warning,
            }
        except Exception as e:
            self._log_tool("execute_trade", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    def close_trade(self, ticket: int, caller_role: str = "floki") -> Dict[str, Any]:
        start = time.time()
        try:
            try:
                t = int(ticket)
            except Exception:
                return {"success": False, "reason": "invalid ticket"}

            # FLO-403 Phase 1 — Snow ownership guard.
            # FLO-403 Phase 2 Step 5 (Q10.1 Option A): caller-aware guard.
            # Default caller_role="floki" preserves Phase 1 behavior;
            # Trade Manager passes caller_role="trade_manager" to bypass
            # (it IS Snow's authorized executor under the new architecture).
            # Failure-safe: if the position lookup raises, assume NOT
            # Snow-owned (false negative over false positive — the existing
            # close_position path stays the dominant one).
            try:
                _positions = self._executor.get_open_positions() or []
                _snow_owned = any(
                    getattr(p, "ticket", None) == t
                    and str(getattr(p, "comment", "") or "").startswith("snow:")
                    for p in _positions
                )
            except Exception:
                _snow_owned = False
            if _snow_owned and caller_role != "trade_manager":
                self._log_tool("close_trade", start, f"ticket={t} | blocked | snow_owned | caller={caller_role}")
                return {
                    "success": False,
                    "reason": "snow_owned",
                    "hint": (
                        "this position is managed by Snow; use cancel_plan "
                        "to close the plan, or let Snow's exit / management "
                        "contingencies fire"
                    ),
                }

            res = self._executor.close_position(t)
            if not getattr(res, "success", False):
                reason = getattr(res, "error_message", None) or "close failed"
                self._log_tool("close_trade", start, f"ticket={t} | success=false | {reason}")
                return {"success": False, "reason": str(reason)}

            close_price = self._safe_float(getattr(res, "price", None))
            self._log_tool("close_trade", start, f"ticket={t} | success")
            return {"success": True, "close_price": close_price, "profit": None}
        except Exception as e:
            self._log_tool("close_trade", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    # -----------------------------------------------------------------
    # FLO-141: adjust_trade guards
    # -----------------------------------------------------------------

    @staticmethod
    def _is_sl_widening(direction_type: int, old_sl: float, new_sl: float) -> bool:
        """Check if new SL widens risk (moves SL further from entry).
        direction_type: 0=BUY, 1=SELL (MT5 convention)."""
        if direction_type == 0:  # BUY — SL is below entry, widening = moving lower
            return new_sl < old_sl
        else:  # SELL — SL is above entry, widening = moving higher
            return new_sl > old_sl

    @staticmethod
    def _is_adjust_rate_limited(ticket: int, max_per_hour: int = 3) -> bool:
        """Check if ticket has exceeded max adjustments in the last rolling hour."""
        now = time.time()
        cutoff = now - 3600
        history = _adjust_rate_history.get(ticket, [])
        # Prune stale entries
        history = [ts for ts in history if ts > cutoff]
        _adjust_rate_history[ticket] = history
        return len(history) >= max_per_hour

    @staticmethod
    def _record_adjustment(ticket: int) -> None:
        """Record a successful adjustment timestamp."""
        _adjust_rate_history.setdefault(ticket, []).append(time.time())

    def adjust_trade(self, ticket: int, new_sl: float, new_tp: float, caller_role: str = "floki") -> Dict[str, Any]:
        """Adjust SL/TP on an open position with SL-widening guard and rate limiting (FLO-141).

        FLO-403 Phase 2 Step 5 (Q10.1 Option A): caller_role gates the
        Snow ownership guard. Default "floki" preserves Phase 1 behavior;
        Trade Manager passes "trade_manager" to bypass.
        """
        start = time.time()
        try:
            try:
                t = int(ticket)
            except Exception:
                return {"success": False, "reason": "invalid ticket"}

            if t <= 0:
                self._log_tool("adjust_trade", start, f"ticket={t} | blocked | invalid_ticket")
                return {"success": False, "reason": "invalid ticket"}

            sl_f = self._safe_float(new_sl)
            tp_f = self._safe_float(new_tp)
            if sl_f is None and tp_f is None:
                return {"success": False, "reason": "invalid new sl/tp"}

            # FLO-200: adjust rate limit REMOVED — Floki has full autonomy
            # (was: 3/hour max, cost $22 on 2026-04-02 when blocked at 15:44)

            # --- Get current position (live MT5) for old values + direction ---
            # FLO-403 Phase 1 — Snow ownership guard piggybacks on this same
            # positions fetch (no extra MT5 round-trip). Same failure-safe
            # default as close_trade: lookup raises → assume NOT Snow-owned.
            old_sl = None
            old_tp = None
            direction_type = None  # 0=BUY, 1=SELL
            _snow_owned = False
            try:
                positions = self._executor.get_open_positions() or []
                for p in positions:
                    if getattr(p, "ticket", None) == t:
                        old_sl = self._safe_float(getattr(p, "sl", None))
                        old_tp = self._safe_float(getattr(p, "tp", None))
                        direction_type = getattr(p, "type", None)
                        if str(getattr(p, "comment", "") or "").startswith("snow:"):
                            _snow_owned = True
                        break
            except Exception:
                pass

            if _snow_owned and caller_role != "trade_manager":
                self._log_tool("adjust_trade", start, f"ticket={t} | blocked | snow_owned | caller={caller_role}")
                return {
                    "success": False,
                    "reason": "snow_owned",
                    "hint": (
                        "this position is managed by Snow; SL / TP changes "
                        "must come from Snow's plan-defined contingencies — "
                        "if the plan is wrong, cancel_plan and submit a new "
                        "plan with the corrected geometry"
                    ),
                }

            # FLO-200: SL widening guard REMOVED — Floki has full autonomy
            # (was: blocked SL moves further from entry)

            # --- Execute modification ---
            res = self._executor.modify_position(t, new_sl=sl_f, new_tp=tp_f)
            if not getattr(res, "success", False):
                reason = getattr(res, "error_message", None) or "adjust failed"
                self._log_tool("adjust_trade", start, f"ticket={t} | success=false | {reason}")
                return {"success": False, "reason": str(reason)}

            # Record successful adjustment for rate limiting
            self._record_adjustment(t)

            # FLO-269: Record SL/TP adjustment for post-trade report
            try:
                from db_writer import record_trade_adjustment
                record_trade_adjustment(
                    ticket=t, old_sl=old_sl, new_sl=sl_f,
                    old_tp=old_tp, new_tp=tp_f, source="floki_adjust",
                )
            except Exception:
                pass

            _fmt = lambda v: f"{v:.2f}" if v is not None else "—"
            log.info(
                f"ADJUST_TRADE | SL: {_fmt(old_sl)}→{_fmt(sl_f)} | "
                f"TP: {_fmt(old_tp)}→{_fmt(tp_f)} | ticket={t}"
            )

            self._log_tool("adjust_trade", start, f"ticket={t} | success")
            return {
                "success": True,
                "ticket": t,
                "new_sl": sl_f,
                "new_tp": tp_f,
            }
        except Exception as e:
            self._log_tool("adjust_trade", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    # ---------------------------------------------------------------------
    # Session memory tools
    # ---------------------------------------------------------------------

    def read_session_memory(self) -> Dict[str, Any]:
        start = time.time()
        try:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            data_dir = os.path.join(base_dir, "data")
            mem_path = os.path.join(data_dir, "agent_session_memory.json")

            if not os.path.exists(mem_path):
                self._log_tool("read_session_memory", start, "empty")
                return {"empty": True}

            try:
                with open(mem_path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
            except Exception:
                self._log_tool("read_session_memory", start, "error=invalid_json")
                return {"empty": True}

            if not isinstance(payload, dict) or not payload:
                self._log_tool("read_session_memory", start, "empty")
                return {"empty": True}

            try:
                # FLO-309: session boundary uses UTC midnight via
                # trading_day_utc (was local midnight from datetime.now()).
                # For CEST users that shifts the rollover ~2h earlier.
                from tz_utils import trading_day_utc as _tday
                today = _tday()
                if str(payload.get("session_date") or "") != today:
                    payload["session_date"] = today
                    payload["notes"] = []
                    payload["last_updated"] = utc_iso()
            except Exception:
                pass

            self._log_tool("read_session_memory", start)
            return payload
        except Exception as e:
            self._log_tool("read_session_memory", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    def get_trade_patterns(self) -> Dict[str, Any]:
        """Return learned pattern memory + context + counter-examples.

        This reads the latest patterns JSON produced by the reflection engine.
        """
        start = time.time()
        try:
            try:
                from agent_reflection import read_patterns
            except Exception:
                self._log_tool("get_trade_patterns", start, "error=import_failed")
                return {"success": False, "reason": "patterns_unavailable"}

            payload = read_patterns()
            if not isinstance(payload, dict) or not payload:
                self._log_tool("get_trade_patterns", start, "error=invalid_payload")
                return {"success": False, "reason": "patterns_unavailable"}

            if payload.get("success") is False:
                self._log_tool("get_trade_patterns", start, f"reason={payload.get('reason')}")
                return payload

            patterns = payload.get("patterns") if isinstance(payload.get("patterns"), list) else []

            context = self._extract_context_for_patterns()
            counter_examples = self._query_similar_losing_trades(context, limit=2)

            out = dict(payload)
            out["context"] = context
            out["counter_examples"] = counter_examples

            self._log_tool(
                "get_trade_patterns",
                start,
                f"patterns={len(patterns)} counter_examples={len(counter_examples)}",
            )
            return out
        except Exception as e:
            self._log_tool("get_trade_patterns", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    def save_lesson(self, text: str, context: Dict[str, Any] = None) -> Dict[str, Any]:
        """FLO-325: Append a permanent lesson to floki_lessons.json.

        Lessons survive restarts and day rollovers. Different from
        session_memory (daily) and get_trade_lessons (bucket outcomes).
        If text matches an existing lesson, its position is bumped to
        newest and its id is reused. FIFO cap 50.
        """
        start = time.time()
        try:
            from floki_lessons import save_lesson as _save
            lid = _save(str(text or ""), context if isinstance(context, dict) else None)
            if lid is None:
                self._log_tool("save_lesson", start, "empty_text_or_save_failed")
                return {"success": False, "reason": "empty_text_or_save_failed"}
            self._log_tool("save_lesson", start, f"id={lid}")
            return {"success": True, "id": lid}
        except Exception as e:
            self._log_tool("save_lesson", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    def forget_lesson(self, lesson_id: int) -> Dict[str, Any]:
        """FLO-325: Remove a lesson by id from floki_lessons.json."""
        start = time.time()
        try:
            from floki_lessons import forget_lesson as _forget
            ok = _forget(int(lesson_id))
            self._log_tool("forget_lesson", start, f"id={lesson_id} removed={ok}")
            return {"success": True, "removed": bool(ok)}
        except Exception as e:
            self._log_tool("forget_lesson", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    def get_trade_lessons(self) -> Dict[str, Any]:
        """Return dynamic lessons from past trades (FLO-63)."""
        start = time.time()
        try:
            from trade_lessons import get_relevant_lessons
            lessons = get_relevant_lessons(min_occurrences=3, limit=10)
            self._log_tool("get_trade_lessons", start, f"lessons={len(lessons)}")
            return {
                "success": True,
                "lessons": lessons,
                "total": len(lessons),
            }
        except Exception as e:
            self._log_tool("get_trade_lessons", start, f"error={e}")
            return {"success": False, "reason": "lessons_unavailable"}

    # -----------------------------------------------------------------
    # FLO-137: Trade reflexion tools
    # -----------------------------------------------------------------

    def get_recent_reflexions(self, limit: int = 5) -> Dict[str, Any]:
        """Return the most recent post-trade reflexions (FLO-137)."""
        start = time.time()
        try:
            from db_writer import get_recent_reflexions as _get
            lim = min(max(int(limit or 5), 1), 20)
            rows = _get(lim)
            self._log_tool("get_recent_reflexions", start, f"count={len(rows)}")
            return {"success": True, "reflexions": rows, "count": len(rows)}
        except Exception as e:
            self._log_tool("get_recent_reflexions", start, f"error={e}")
            return {"success": False, "reason": "reflexions_unavailable"}

    def search_reflexions(self, keywords: str, limit: int = 5) -> Dict[str, Any]:
        """Search past trade reflexions by keywords (FLO-138)."""
        start = time.time()
        try:
            from db_writer import search_reflexions as _search
            kw = str(keywords or "").strip()
            if not kw:
                return {"success": False, "reason": "empty keywords"}
            lim = min(max(int(limit or 5), 1), 20)
            rows = _search(kw, lim)
            self._log_tool("search_reflexions", start, f"keywords={kw} | count={len(rows)}")
            return {"success": True, "results": rows, "count": len(rows)}
        except Exception as e:
            self._log_tool("search_reflexions", start, f"error={e}")
            return {"success": False, "reason": "search_unavailable"}

    def search_memory(self, query: str, limit: int = 3) -> Dict[str, Any]:
        """Semantic search across trade reflexions using embeddings (FLO-138 Phase 2)."""
        start = time.time()
        try:
            from trade_reflexion import search_memory as _semantic_search
            q = str(query or "").strip()
            if not q:
                return {"success": False, "reason": "empty query"}
            lim = min(max(int(limit or 3), 1), 10)
            results = _semantic_search(q, lim)
            if not results:
                # Fallback hint
                self._log_tool("search_memory", start, "chromadb_empty_or_unavailable")
                return {
                    "success": False,
                    "reason": "chromadb_unavailable",
                    "fallback": "use search_reflexions for keyword search",
                }
            self._log_tool("search_memory", start, f"query={q[:50]} | count={len(results)}")
            return {"success": True, "results": results, "count": len(results)}
        except Exception as e:
            self._log_tool("search_memory", start, f"error={e}")
            return {
                "success": False,
                "reason": "search_memory_error",
                "fallback": "use search_reflexions for keyword search",
            }

    # -----------------------------------------------------------------
    # FLO-269: Trade Journal — full trade history with MFE/MAE/adjustments
    # -----------------------------------------------------------------

    def get_trade_journal(
        self, limit: int = 20, session_filter: str = "", direction_filter: str = ""
    ) -> Dict[str, Any]:
        """Return detailed trade journal with MFE, capture rate, SL adjustments, and counterfactuals."""
        start = time.time()
        try:
            import json as _json
            import os as _os
            import sqlite3
            import config as _cfg
            from db_writer import _get_connection, get_trade_adjustments

            lim = min(max(int(limit or 20), 1), 30)

            conn = _get_connection()
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT ticket, direction, volume, open_price, close_price, sl, tp, "
                "profit, close_reason, open_time, close_time, mfe_points, mae_points, "
                "final_sl, breakeven_activated, decision_source, comment "  # FLO-301: comment for entry_type
                "FROM trades WHERE close_price IS NOT NULL AND profit IS NOT NULL "
                "ORDER BY close_time DESC LIMIT ?",
                (lim * 2,),  # fetch extra to allow filtering
            ).fetchall()

            # FLO-300: pull OPEN_* decisions once so we can attach opening
            # confidence to each trade below without N extra DB roundtrips.
            try:
                _open_decisions = conn.execute(
                    "SELECT timestamp, agent_decision, agent_confidence "
                    "FROM agent_proactive_analyses "
                    "WHERE agent_decision IN ('OPEN_BUY', 'OPEN_SELL') "
                    "ORDER BY timestamp ASC"
                ).fetchall()
            except Exception:
                _open_decisions = []
            conn.close()

            from datetime import datetime as _dt
            def _find_open_conf(direction, open_time_iso):
                if not direction or not open_time_iso:
                    return None
                want = "OPEN_BUY" if str(direction).upper() == "BUY" else "OPEN_SELL"
                try:
                    t_target = _dt.fromisoformat(str(open_time_iso).rstrip("Z").split(".")[0])
                except Exception:
                    return None
                best = None; best_delta = None
                for d in _open_decisions:
                    if d["agent_decision"] != want:
                        continue
                    try:
                        t_dec = _dt.fromisoformat(str(d["timestamp"]).rstrip("Z").split(".")[0])
                    except Exception:
                        continue
                    delta = abs((t_target - t_dec).total_seconds())
                    if delta > 600:
                        continue
                    if best_delta is None or delta < best_delta:
                        best_delta = delta; best = d["agent_confidence"]
                return best

            # Session helper (same as sage_auditor corrected logic)
            _offset = int(getattr(_cfg, "MT5_SERVER_UTC_OFFSET", 2) or 2)

            def _session(ts):
                try:
                    from datetime import datetime as _dt
                    d = _dt.fromisoformat((ts or "").split(".")[0])
                    h = (d.hour - _offset) % 24
                    if 0 <= h < 7:
                        return "Asian"
                    if 7 <= h < 13:
                        return "London"
                    if 13 <= h < 22:
                        return "NY"
                    return "OffHours"
                except Exception:
                    return "?"

            # Filter + build
            sess_f = (session_filter or "").strip().upper()
            dir_f = (direction_filter or "").strip().upper()
            reports_dir = _os.path.join(
                _os.path.dirname(_os.path.abspath(__file__)), "data", "post_trade_reports"
            )

            trades_xml = []
            total_capture = []
            adj_helped = 0
            adj_hurt = 0
            adj_neutral = 0
            count = 0
            _conf_outcomes = []   # FLO-300: (open_conf, pnl) pairs for band stats
            _et_outcomes = {"MARKET": [0, 0], "PENDING": [0, 0]}  # FLO-301: [trades, wins]

            for r in rows:
                if count >= lim:
                    break
                t = dict(r)
                ticket = t["ticket"]
                direction = t.get("direction", "?")
                sess_open = _session(t.get("open_time"))
                sess_close = _session(t.get("close_time"))

                if sess_f and sess_f not in (sess_open.upper(), sess_close.upper()):
                    continue
                if dir_f and direction.upper() != dir_f:
                    continue

                count += 1
                pnl = t.get("profit") or 0
                mfe = t.get("mfe_points")
                mae = t.get("mae_points")
                final_sl = t.get("final_sl")
                orig_sl = t.get("sl")

                # Capture rate — FLO-290: pips/pips (was dollars/pips bug).
                # FLO-300: display helper clamps extremes and shows "LOSS" for
                # noise-floor-small-MFE losses (previously rendered as "-6100%").
                from capture import compute_capture_pct, pnl_pips as _pnl_pips, format_capture_display
                capture = compute_capture_pct(
                    direction=t.get("direction"),
                    entry_price=t.get("open_price"),
                    close_price=t.get("close_price"),
                    mfe_points=mfe,
                )
                _pp = _pnl_pips(t.get("direction"), t.get("open_price"), t.get("close_price"))
                _capture_str = format_capture_display(capture, mfe, _pp)
                if capture is not None and mfe is not None and mfe > 0:
                    total_capture.append(capture)
                # FLO-301: detect PENDING vs MARKET from MT5 comment column.
                _cmt = (t.get("comment") or "")
                _entry_type = "PENDING" if ("pending" in _cmt.lower()) else "MARKET"
                _et_outcomes[_entry_type][0] += 1
                if (pnl or 0) > 0:
                    _et_outcomes[_entry_type][1] += 1

                # FLO-300/301: opening confidence ONLY for market orders. Pending
                # fills happen hours after the decision → attributing confidence
                # is misleading, so open_conf=None and XML renders "P.O."
                if _entry_type == "MARKET":
                    _open_conf = _find_open_conf(t.get("direction"), t.get("open_time"))
                    if _open_conf is not None:
                        _conf_outcomes.append((int(_open_conf), float(pnl or 0)))
                else:
                    _open_conf = None

                # Adjustments
                adjustments = get_trade_adjustments(int(ticket))
                if adjustments:
                    orig_sl = adjustments[0].get("old_sl") or orig_sl

                # Duration
                dur = ""
                try:
                    from datetime import datetime as _dt
                    od = _dt.fromisoformat((t.get("open_time") or "").split(".")[0])
                    cd = _dt.fromisoformat((t.get("close_time") or "").split(".")[0])
                    dur = f"{round((cd - od).total_seconds() / 60)}min"
                except Exception:
                    pass

                # Load counterfactual + MFE snapshot from report JSON
                cf = None
                mfe_snap = None
                report_path = _os.path.join(reports_dir, f"{ticket}.json")
                if _os.path.exists(report_path):
                    try:
                        with open(report_path, "r", encoding="utf-8") as f:
                            report_data = _json.load(f)
                        cf = report_data.get("counterfactual")
                        mfe_snap = report_data.get("mfe_snapshot")
                    except Exception:
                        pass

                # Verdict: compare actual outcome to counterfactual
                # Skip if no valid SL data (orig_sl=0 or None = reconciled trade with missing data)
                verdict = ""
                if cf and orig_sl is not None and float(orig_sl) > 0:
                    sl_survived = cf.get("original_sl_survived")
                    tp_hit = cf.get("tp_would_have_been_hit")
                    tp_pnl = cf.get("tp_hit_pnl")
                    entry_f = float(t.get("open_price") or 0)

                    if sl_survived is False and entry_f > 0:
                        # Original SL would have been hit — compute P&L if held to SL
                        orig_sl_f = float(orig_sl)
                        if direction.upper() == "BUY":
                            pnl_if_original = orig_sl_f - entry_f  # negative (loss)
                        else:
                            pnl_if_original = entry_f - orig_sl_f  # negative (loss)
                        diff = round(float(pnl) - pnl_if_original, 2)
                        if diff > 0:
                            verdict = f"SAVED ${diff:.2f}"
                            adj_helped += 1
                        elif diff < 0:
                            verdict = f"COST ${abs(diff):.2f}"
                            adj_hurt += 1
                        else:
                            verdict = "NEUTRAL"
                            adj_neutral += 1
                    elif tp_hit and tp_pnl is not None:
                        # TP would have been hit = actual close left money on table
                        cost = round(float(tp_pnl) - float(pnl), 2)
                        if cost > 0:
                            verdict = f"COST ${cost:.2f}"
                            adj_hurt += 1
                        else:
                            verdict = "NEUTRAL"
                            adj_neutral += 1
                    elif sl_survived is True and not tp_hit:
                        verdict = "NEUTRAL"
                        adj_neutral += 1

                # Format trade XML.
                # FLO-300: capture uses display helper (clamped / "LOSS").
                # FLO-301: entry_type + open_conf distinguishes pending orders
                # ("P.O.") from market orders ("52%"). Pending-order confidence
                # isn't comparable because the fill happens hours after decision.
                _f = lambda v, d=2: f"{float(v):.{d}f}" if v is not None else "?"
                if _entry_type == "PENDING":
                    _oc_str = "P.O."
                else:
                    _oc_str = f"{int(_open_conf)}%" if _open_conf is not None else "?"
                line = (
                    f'  <trade ticket="{ticket}" dir="{direction}" entry_type="{_entry_type}" '
                    f'session="{sess_open}->{sess_close}" '
                    f'pnl="${_f(pnl)}" mfe="{_f(mfe, 1)}pts" mae="{_f(mae, 1)}pts" '
                    f'capture="{_capture_str}" open_conf="{_oc_str}" '
                    f'entry="{_f(t.get("open_price"))}" orig_sl="{_f(orig_sl)}" '
                    f'final_sl="{_f(final_sl)}" tp="{_f(t.get("tp"))}" '
                    f'close="{_f(t.get("close_price"))}" type="{t.get("close_reason", "?")}" '
                    f'duration="{dur}"'
                )

                # Pre-check whether MFE snapshot will be rendered (same filter as below)
                _will_show_mfe = bool(
                    mfe_snap and (pnl < 0 or (capture is not None and capture < 50))
                )

                if not adjustments and not cf and not _will_show_mfe:
                    line += "/>"
                    trades_xml.append(line)
                    continue

                line += ">"
                trades_xml.append(line)

                # Adjustments sub-elements
                if adjustments:
                    trades_xml.append(f'    <adjustments count="{len(adjustments)}">')
                    for a in adjustments:
                        mins = ""
                        try:
                            from datetime import datetime as _dt
                            ot = _dt.fromisoformat((t.get("open_time") or "").split(".")[0])
                            at = _dt.fromisoformat((a.get("timestamp") or "").split(".")[0])
                            mins = f'{round((at - ot).total_seconds() / 60)}min'
                        except Exception:
                            pass
                        sl_part = f'sl="{_f(a.get("old_sl"))}->{_f(a.get("new_sl"))}"'
                        tp_part = ""
                        if a.get("new_tp") is not None and a.get("old_tp") != a.get("new_tp"):
                            tp_part = f' tp="{_f(a.get("old_tp"))}->{_f(a.get("new_tp"))}"'
                        trades_xml.append(
                            f'      <adj at="{mins}" {sl_part}{tp_part} source="{a.get("source", "?")}"/>'
                        )
                    trades_xml.append("    </adjustments>")

                # Counterfactual (rich detail)
                if cf:
                    cf_attrs = []
                    if cf.get("original_sl_survived") is True:
                        cf_attrs.append('orig_sl="survived"')
                    elif cf.get("original_sl_survived") is False:
                        hit_time = cf.get("original_sl_hit_time", "?")
                        # Extract just HH:MM from ISO timestamp
                        try:
                            hit_time = hit_time[11:16]
                        except Exception:
                            pass
                        cf_attrs.append(f'orig_sl="hit at {hit_time}"')
                    if cf.get("tp_would_have_been_hit"):
                        tp_time = cf.get("tp_hit_time", "?")
                        try:
                            tp_time = tp_time[11:16]
                        except Exception:
                            pass
                        cf_attrs.append(f'tp="hit at {tp_time} = +${cf.get("tp_hit_pnl")}"')
                    elif cf.get("tp_reached_after_sl"):
                        after_time = cf.get("tp_reached_after_sl_time", "?")
                        after_pnl = cf.get("tp_reached_after_sl_pnl")
                        try:
                            after_time = after_time[11:16]
                        except Exception:
                            pass
                        cf_attrs.append(f'tp="reached at {after_time} = +${after_pnl} BUT after SL hit"')
                    else:
                        cf_attrs.append('tp="never reached"')
                    hours = cf.get("hours_of_data", 0)
                    cf_attrs.append(f'window="{hours:.0f}h"')
                    cf_attrs.append(f'verdict="{verdict}"')
                    trades_xml.append(f'    <counterfactual {" ".join(cf_attrs)}/>')

                # FLO-273: MFE snapshot — show indicator state at peak profit
                # Only for losing trades or trades with low capture rate (where MFE matters)
                show_mfe = False
                if mfe_snap:
                    if pnl < 0:
                        show_mfe = True
                    elif capture is not None and capture < 50:
                        show_mfe = True

                if show_mfe and mfe_snap:
                    m_attrs = []
                    _mfe_pips = mfe_snap.get("profit_pips")
                    if _mfe_pips is not None:
                        m_attrs.append(f'at="+{_mfe_pips}pts"')
                    _mfe_time = mfe_snap.get("timestamp", "")
                    try:
                        _mfe_time_short = _mfe_time[11:16] if _mfe_time else ""
                        if _mfe_time_short:
                            m_attrs.append(f'time="{_mfe_time_short}"')
                    except Exception:
                        pass
                    if mfe_snap.get("rsi") is not None:
                        m_attrs.append(f'rsi="{mfe_snap["rsi"]}"')
                    _sk = mfe_snap.get("stochastic_k")
                    _sd = mfe_snap.get("stochastic_d")
                    if _sk is not None and _sd is not None:
                        m_attrs.append(f'stoch="{_sk}/{_sd}"')
                    elif _sk is not None:
                        m_attrs.append(f'stoch="{_sk}"')
                    if mfe_snap.get("adx") is not None:
                        m_attrs.append(f'adx="{mfe_snap["adx"]}"')
                    if mfe_snap.get("volume_ratio") is not None:
                        m_attrs.append(f'vol="{mfe_snap["volume_ratio"]}x"')
                    if mfe_snap.get("macd_histogram") is not None:
                        m_attrs.append(f'macd_h="{mfe_snap["macd_histogram"]}"')
                    if mfe_snap.get("bb_position"):
                        m_attrs.append(f'bb="{mfe_snap["bb_position"]}"')
                    if mfe_snap.get("nearest_sr"):
                        m_attrs.append(f'sr="{mfe_snap["nearest_sr"]}"')
                    if mfe_snap.get("regime"):
                        m_attrs.append(f'regime="{mfe_snap["regime"]}"')
                    _fd = mfe_snap.get("floki_decision_at_mfe")
                    _fc = mfe_snap.get("floki_confidence_at_mfe")
                    if _fd:
                        _fc_str = f" ({_fc}%)" if _fc is not None else ""
                        m_attrs.append(f'floki_said="{_fd}{_fc_str}"')
                    trades_xml.append(f'    <mfe_snapshot {" ".join(m_attrs)}/>')

                trades_xml.append("  </trade>")

            # Header stats
            avg_cap = round(sum(total_capture) / len(total_capture), 1) if total_capture else None
            total_adj_trades = adj_helped + adj_hurt + adj_neutral
            helped_pct = round(adj_helped / total_adj_trades * 100) if total_adj_trades > 0 else None
            hurt_pct = round(adj_hurt / total_adj_trades * 100) if total_adj_trades > 0 else None

            # FLO-300: win-rate-by-confidence-band summary for Floki to learn from.
            # Bands: <50, 50-65, 65+. "Win" = pnl > 0.
            _bands = {"lt50": [0,50,0,0], "mid": [50,65,0,0], "ge65": [65,101,0,0]}
            for _c, _p in _conf_outcomes:
                for _b in _bands.values():
                    if _b[0] <= _c < _b[1]:
                        _b[2] += 1           # trades
                        if _p > 0: _b[3] += 1  # wins
                        break
            def _band_attr(b):
                if not b[2]: return None
                return f"{b[3]}/{b[2]} ({round(b[3]/b[2]*100)}%)"
            _lt50 = _band_attr(_bands["lt50"])
            _mid  = _band_attr(_bands["mid"])
            _ge65 = _band_attr(_bands["ge65"])

            header = f'<trade_journal count="{count}"'
            if avg_cap is not None:
                header += f' avg_capture="{avg_cap}%"'
            if helped_pct is not None:
                header += f' adj_helped="{helped_pct}%" adj_hurt="{hurt_pct}%"'
            # FLO-301: market vs pending win-rate split, so Floki can compare
            # whether his pending orders perform better or worse than market
            # orders. Bands below are market-only (pending have no comparable
            # confidence value — see FLO-301 rationale).
            def _wr_attr(tw_pair):
                tr, wn = tw_pair
                return f"{wn}/{tr} ({round(wn/tr*100)}%)" if tr else None
            _mkt_wr = _wr_attr(_et_outcomes["MARKET"])
            _pnd_wr = _wr_attr(_et_outcomes["PENDING"])
            if _mkt_wr is not None: header += f' market_wr="{_mkt_wr}"'
            if _pnd_wr is not None: header += f' pending_wr="{_pnd_wr}"'
            # FLO-300: band breakdown (market orders only) — only emit bands that
            # have data, so Floki isn't misled by "0/0" slots.
            if _lt50 is not None: header += f' wr_lt50="{_lt50}"'
            if _mid  is not None: header += f' wr_50_65="{_mid}"'
            if _ge65 is not None: header += f' wr_65_plus="{_ge65}"'
            header += ">"

            xml = header + "\n" + "\n".join(trades_xml) + "\n</trade_journal>"

            self._log_tool("get_trade_journal", start, f"count={count} avg_cap={avg_cap}")
            return {"success": True, "journal": xml, "count": count}

        except Exception as e:
            self._log_tool("get_trade_journal", start, f"error={e}")
            return {"success": False, "reason": f"journal_error: {e}"}

    # -----------------------------------------------------------------
    # FLO-281: Position history — indicator trajectory for an open trade
    # -----------------------------------------------------------------

    def get_position_history(self, ticket: int) -> Dict[str, Any]:
        """Return compact XML summary of how an open position has performed.

        Queries trade_snapshots for profit range, duration, trend direction,
        current indicators, and indicators at MFE peak. Floki calls this
        when he wants to review how his trade has been trending.
        """
        start = time.time()
        try:
            try:
                t = int(ticket)
            except Exception:
                self._log_tool("get_position_history", start, f"invalid ticket={ticket}")
                return {"success": False, "reason": "invalid ticket"}
            if t <= 0:
                self._log_tool("get_position_history", start, f"ticket={t} invalid")
                return {"success": False, "reason": "invalid ticket"}

            import sqlite3 as _sql
            import config as _cfg
            from datetime import datetime as _dt

            db_path = os.path.abspath(getattr(_cfg, "HISTORY_DB_PATH", "data/history.db"))
            conn = _sql.connect(db_path, timeout=5)
            conn.row_factory = _sql.Row

            # Aggregate stats
            agg = conn.execute(
                "SELECT COUNT(*) as n, MIN(profit_pips) as min_p, MAX(profit_pips) as max_p, "
                "MIN(timestamp) as first_ts, MAX(timestamp) as last_ts "
                "FROM trade_snapshots WHERE ticket = ? AND profit_pips IS NOT NULL",
                (t,),
            ).fetchone()
            n = agg["n"] if agg else 0
            if not n:
                conn.close()
                self._log_tool("get_position_history", start, f"ticket={t} no snapshots")
                return {"success": False, "reason": f"No snapshot history for ticket {t} — position may have just opened"}

            min_p = float(agg["min_p"]) if agg["min_p"] is not None else 0.0
            max_p = float(agg["max_p"]) if agg["max_p"] is not None else 0.0
            first_ts = agg["first_ts"]
            last_ts = agg["last_ts"]

            # MFE snapshot row (for indicators at peak)
            mfe_row = conn.execute(
                "SELECT * FROM trade_snapshots WHERE ticket = ? AND profit_pips IS NOT NULL "
                "ORDER BY profit_pips DESC LIMIT 1",
                (t,),
            ).fetchone()
            mfe_snap = dict(mfe_row) if mfe_row else {}

            # Most recent snapshot (for indicators now)
            now_row = conn.execute(
                "SELECT * FROM trade_snapshots WHERE ticket = ? "
                "ORDER BY timestamp DESC LIMIT 1",
                (t,),
            ).fetchone()
            now_snap = dict(now_row) if now_row else {}
            conn.close()

            current_p = float(now_snap.get("profit_pips") or 0.0)

            # Duration from first to last snapshot
            duration_str = "?"
            try:
                fd = _dt.fromisoformat(str(first_ts).replace("Z", "+00:00")) if first_ts else None
                ld = _dt.fromisoformat(str(last_ts).replace("Z", "+00:00")) if last_ts else None
                if fd and ld:
                    mins = int((ld - fd).total_seconds() / 60)
                    duration_str = f"{mins}m" if mins < 60 else f"{mins // 60}h{mins % 60}m"
            except Exception:
                pass

            # Simple trend classification — FLO-289: all numbers explicitly pips.
            range_p = max_p - min_p
            if range_p < 10:
                trend_dir = "flat"
                trend_desc = f"Oscillating between {min_p:+.1f} and {max_p:+.1f} pips for {duration_str}. No directional progress."
            elif current_p >= max_p * 0.8 and max_p > 0:
                trend_dir = "climbing"
                trend_desc = f"Near peak profit ({max_p:+.1f} pips). Currently {current_p:+.1f} pips."
            elif current_p <= min_p * 0.8 and min_p < 0:
                trend_dir = "losing_ground"
                trend_desc = f"Near worst drawdown ({min_p:+.1f} pips). Currently {current_p:+.1f} pips."
            elif max_p > 0 and current_p < max_p * 0.3:
                trend_dir = "gave_back_gains"
                trend_desc = f"Peaked at {max_p:+.1f} pips but fell back to {current_p:+.1f} pips. Gave back {max_p - current_p:.1f} pips."
            else:
                trend_dir = "mixed"
                trend_desc = f"Range {min_p:+.1f} to {max_p:+.1f} pips, currently {current_p:+.1f} pips."

            # Build XML
            def _attr(v):
                return "?" if v is None else str(v)

            def _fmt_num(v, d=1):
                try:
                    return f"{float(v):.{d}f}" if v is not None else "?"
                except Exception:
                    return "?"

            mfe_time_short = "?"
            try:
                _mt = str(mfe_snap.get("timestamp") or "")
                mfe_time_short = _mt[11:16] if _mt else "?"
            except Exception:
                pass

            lines = [
                f'<position_history ticket="{t}" duration="{duration_str}" snapshots="{n}">',
                # FLO-289: unit="pips" explicit — prevents Floki from reading
                # these as dollars (the unlabeled 63.8 was misread as "$63 peak").
                f'  <profit_range unit="pips" min="{_fmt_num(min_p)}" max="{_fmt_num(max_p)}" current="{_fmt_num(current_p)}"/>',
                f'  <trend direction="{trend_dir}" description="{trend_desc}"/>',
                f'  <indicators_now rsi="{_attr(now_snap.get("rsi"))}" '
                f'stoch_k="{_attr(now_snap.get("stochastic_k"))}" '
                f'adx="{_attr(now_snap.get("adx"))}" '
                f'regime="{_attr(now_snap.get("regime"))}" '
                f'nearest_sr="{_attr(now_snap.get("nearest_sr"))}"/>',
                f'  <indicators_at_mfe rsi="{_attr(mfe_snap.get("rsi"))}" '
                f'stoch_k="{_attr(mfe_snap.get("stochastic_k"))}" '
                f'adx="{_attr(mfe_snap.get("adx"))}" '
                f'regime="{_attr(mfe_snap.get("regime"))}" '
                f'at="{_fmt_num(mfe_snap.get("profit_pips"))}pts" '
                f'time="{mfe_time_short}"/>',
                "</position_history>",
            ]
            xml = "\n".join(lines)

            self._log_tool("get_position_history", start,
                           f"ticket={t} snapshots={n} range={min_p:+.1f}..{max_p:+.1f} trend={trend_dir}")
            return {"success": True, "history": xml, "snapshots": n}

        except Exception as e:
            self._log_tool("get_position_history", start, f"error={e}")
            return {"success": False, "reason": f"history_error: {e}"}

    # -----------------------------------------------------------------
    # FLO-158: Rex-unique tools (not available to Floki)
    # -----------------------------------------------------------------

    def rex_session_performance(self) -> Dict[str, Any]:
        """WR and PF by session + direction for recent agent trades."""
        start = time.time()
        try:
            from db_writer import _get_connection
            conn = _get_connection()
            try:
                rows = conn.execute("""
                    SELECT direction, close_reason, profit, open_time
                    FROM trades
                    WHERE close_time IS NOT NULL AND profit IS NOT NULL
                      AND decision_source IN ('floki_agent', 'agent_floki')
                      AND open_time >= datetime('now', '-30 days')
                """).fetchall()
            finally:
                conn.close()

            sessions = {"asian": {}, "london": {}, "ny": {}}
            for direction, _, profit, open_time in rows:
                try:
                    hour = int(open_time[11:13]) if open_time and len(open_time) > 13 else 12
                except Exception:
                    hour = 12
                if 22 <= hour or hour < 7:
                    sess = "asian"
                elif 7 <= hour < 13:
                    sess = "london"
                else:
                    sess = "ny"
                d = str(direction or "BUY").upper()
                key = d
                if key not in sessions[sess]:
                    sessions[sess][key] = {"wins": 0, "losses": 0, "total_pnl": 0.0}
                pnl = float(profit or 0)
                if pnl > 0:
                    sessions[sess][key]["wins"] += 1
                else:
                    sessions[sess][key]["losses"] += 1
                sessions[sess][key]["total_pnl"] += pnl

            result = {}
            for sess, directions in sessions.items():
                result[sess] = {}
                for d, stats in directions.items():
                    n = stats["wins"] + stats["losses"]
                    wr = round(stats["wins"] / n * 100, 1) if n > 0 else 0
                    result[sess][d] = {"wr": wr, "n": n, "pnl": round(stats["total_pnl"], 2)}
            self._log_tool("rex_session_performance", start, f"sessions={len(result)}")
            return {"success": True, "performance": result}
        except Exception as e:
            self._log_tool("rex_session_performance", start, f"error={e}")
            return {"success": False, "reason": str(e)}

    def rex_divergence_scan(self) -> Dict[str, Any]:
        """Scan for RSI/MACD divergences on H4 and D1."""
        start = time.time()
        try:
            from mt5_safe import mt5  # FLO-348
            import numpy as np
            if not mt5.initialize():
                return {"success": False, "reason": "MT5 unavailable"}

            result = {}
            for tf_name, tf in [("H4", mt5.TIMEFRAME_H4), ("D1", mt5.TIMEFRAME_D1)]:
                bars = mt5.copy_rates_from_pos("XAUUSD", tf, 0, 20)
                if bars is None or len(bars) < 10:
                    result[tf_name] = {"rsi": "insufficient_data", "macd": "insufficient_data"}
                    continue

                closes = [float(b[4]) for b in bars]
                highs = [float(b[2]) for b in bars]
                lows = [float(b[3]) for b in bars]

                # RSI calculation (14-period)
                deltas = np.diff(closes)
                gains = np.where(deltas > 0, deltas, 0)
                losses = np.where(deltas < 0, -deltas, 0)
                avg_gain = np.mean(gains[-14:])
                avg_loss = np.mean(losses[-14:])
                rs = avg_gain / avg_loss if avg_loss > 0 else 100
                rsi_now = 100 - (100 / (1 + rs))

                # Check last 2 swing highs for bearish divergence
                rsi_div = "none"
                if len(closes) >= 10:
                    # Simple: compare price high vs RSI at recent peaks
                    ph1_idx = np.argmax(highs[-10:-5])
                    ph2_idx = np.argmax(highs[-5:]) + 5
                    if highs[ph2_idx + len(highs) - 10] > highs[ph1_idx + len(highs) - 10]:
                        # Price higher high — check if RSI lower
                        # Approximate RSI at each peak (simplified)
                        if rsi_now < 60 and closes[-1] > closes[-6]:
                            rsi_div = "bearish"
                    elif highs[ph2_idx + len(highs) - 10] < highs[ph1_idx + len(highs) - 10]:
                        if rsi_now > 40 and closes[-1] < closes[-6]:
                            rsi_div = "bullish"

                # MACD histogram divergence
                macd_div = "none"
                if len(closes) >= 26:
                    ema12_arr = [closes[0]]
                    ema26_arr = [closes[0]]
                    m12 = 2.0 / 13.0
                    m26 = 2.0 / 27.0
                    for c in closes[1:]:
                        ema12_arr.append(c * m12 + ema12_arr[-1] * (1 - m12))
                        ema26_arr.append(c * m26 + ema26_arr[-1] * (1 - m26))
                    macd_line = [e12 - e26 for e12, e26 in zip(ema12_arr, ema26_arr)]
                    signal = [macd_line[0]]
                    m9 = 2.0 / 10.0
                    for v in macd_line[1:]:
                        signal.append(v * m9 + signal[-1] * (1 - m9))
                    hist = [m - s for m, s in zip(macd_line, signal)]
                    # Compare histogram at recent swing highs/lows (last 10 bars split into 2 halves)
                    if len(hist) >= 10:
                        h1_peak = max(hist[-10:-5])
                        h2_peak = max(hist[-5:])
                        h1_trough = min(hist[-10:-5])
                        h2_trough = min(hist[-5:])
                        price_hh = max(highs[-5:]) > max(highs[-10:-5])
                        price_ll = min(lows[-5:]) < min(lows[-10:-5])
                        if price_hh and h2_peak < h1_peak and h1_peak > 0:
                            macd_div = "bearish"
                        elif price_ll and h2_trough > h1_trough and h1_trough < 0:
                            macd_div = "bullish"

                result[tf_name] = {
                    "rsi": rsi_div,
                    "rsi_value": round(rsi_now, 1),
                    "macd_divergence": macd_div,
                    "bars_analyzed": len(bars),
                }
            self._log_tool("rex_divergence_scan", start, f"H4={result.get('H4',{}).get('rsi')} D1={result.get('D1',{}).get('rsi')}")
            return {"success": True, "divergences": result}
        except Exception as e:
            self._log_tool("rex_divergence_scan", start, f"error={e}")
            return {"success": False, "reason": str(e)}

    def rex_regime_history(self) -> Dict[str, Any]:
        """Read regime state history — past transitions and durations."""
        start = time.time()
        try:
            import json as _json
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "regime_state.json")
            if not os.path.exists(path):
                return {"success": True, "current": None, "transitions": []}
            with open(path, "r", encoding="utf-8") as f:
                data = _json.load(f)
            regime = data.get("regime")
            change_ts = data.get("change_ts")
            history = data.get("history", [])[-10:]
            duration_min = None
            if change_ts:
                duration_min = int((time.time() - float(change_ts)) / 60)
            self._log_tool("rex_regime_history", start, f"regime={regime} transitions={len(history)}")
            return {
                "success": True,
                "current_regime": regime,
                "duration_minutes": duration_min,
                "recent_transitions": history,
            }
        except Exception as e:
            self._log_tool("rex_regime_history", start, f"error={e}")
            return {"success": False, "reason": str(e)}

    def rex_reflexion_search(self, query: str, limit: int = 3) -> Dict[str, Any]:
        """Semantic search past trade reflexions (ChromaDB)."""
        start = time.time()
        try:
            from trade_reflexion import search_memory as _search
            q = str(query or "").strip()
            if not q:
                return {"success": False, "reason": "empty query"}
            results = _search(q, min(max(int(limit or 3), 1), 10))
            self._log_tool("rex_reflexion_search", start, f"query={q[:30]} results={len(results)}")
            return {"success": True, "results": results, "count": len(results)}
        except Exception as e:
            self._log_tool("rex_reflexion_search", start, f"error={e}")
            return {"success": False, "reason": str(e)}

    def rex_correlation_check(self) -> Dict[str, Any]:
        """Real-time correlation check: gold vs DXY, yields, silver (last 24h H1)."""
        start = time.time()
        try:
            from mt5_safe import mt5  # FLO-348
            import numpy as np
            if not mt5.initialize():
                return {"success": False, "reason": "MT5 unavailable"}

            pairs = {
                "gold_dxy": ("XAUUSD", "DXY_M6"),
                "gold_silver": ("XAUUSD", "XAGUSD"),
                "gold_10y": ("XAUUSD", "UST10Y_M6"),
            }
            normal_corr = {"gold_dxy": -0.60, "gold_silver": 0.85, "gold_10y": -0.50}
            result = {}
            gold_bars = mt5.copy_rates_from_pos("XAUUSD", mt5.TIMEFRAME_H1, 0, 24)
            if gold_bars is None or len(gold_bars) < 12:
                return {"success": False, "reason": "insufficient gold data"}
            gold_closes = np.array([float(b[4]) for b in gold_bars])

            for key, (_, other_sym) in pairs.items():
                other_bars = mt5.copy_rates_from_pos(other_sym, mt5.TIMEFRAME_H1, 0, 24)
                if other_bars is None or len(other_bars) < 12:
                    result[key] = {"status": "no_data"}
                    continue
                other_closes = np.array([float(b[4]) for b in other_bars])
                min_len = min(len(gold_closes), len(other_closes))
                if min_len < 12:
                    result[key] = {"status": "insufficient_overlap"}
                    continue
                corr = float(np.corrcoef(gold_closes[-min_len:], other_closes[-min_len:])[0, 1])
                norm = normal_corr.get(key, 0)
                broken = abs(corr - norm) > 0.4
                result[key] = {
                    "correlation": round(corr, 3),
                    "normal": norm,
                    "status": "BROKEN" if broken else "NORMAL",
                }
            self._log_tool("rex_correlation_check", start, f"pairs={len(result)}")
            return {"success": True, "correlations": result}
        except Exception as e:
            self._log_tool("rex_correlation_check", start, f"error={e}")
            return {"success": False, "reason": str(e)}

    def write_session_memory(self, thesis: str, note: str) -> Dict[str, Any]:
        start = time.time()
        try:
            thesis_s = str(thesis or "").strip()
            note_s = str(note or "").strip()
            if not thesis_s and not note_s:
                return {"success": False, "reason": "empty thesis/note"}

            base_dir = os.path.dirname(os.path.abspath(__file__))
            data_dir = os.path.join(base_dir, "data")
            mem_path = os.path.join(data_dir, "agent_session_memory.json")
            os.makedirs(data_dir, exist_ok=True)

            # FLO-309: session boundary → trading_day_utc (UTC midnight).
            from tz_utils import trading_day_utc as _tday
            today = _tday()
            payload: Dict[str, Any] = {
                "session_date": today,
                "thesis": thesis_s,
                "trades_today": 0,
                "wins_today": 0,
                "losses_today": 0,
                "notes": [],
                "last_updated": utc_iso(),
            }

            if os.path.exists(mem_path):
                try:
                    with open(mem_path, "r", encoding="utf-8") as f:
                        existing = json.load(f)
                    if isinstance(existing, dict):
                        payload.update(existing)
                except Exception:
                    pass

            # Daily rollover
            if str(payload.get("session_date") or "") != today:
                preserved_sage_notes = []
                try:
                    for n in payload.get("notes") or []:
                        if isinstance(n, dict) and str(n.get("source") or "").strip().lower() == "sage":
                            preserved_sage_notes.append(n)
                except Exception:
                    preserved_sage_notes = []
                payload = {
                    "session_date": today,
                    "thesis": thesis_s,
                    "trades_today": 0,
                    "wins_today": 0,
                    "losses_today": 0,
                    "notes": preserved_sage_notes,
                    "last_updated": utc_iso(),  # FLO-309 regression fix
                }

            if thesis_s:
                payload["thesis"] = thesis_s

            if not isinstance(payload.get("notes"), list):
                payload["notes"] = []

            if note_s:
                # FLO-241: Dedup — ALWAYS check, no exceptions. Reflection forces Floki to think.
                try:
                    import re as _re_sm
                    _SYN = {"middle": "center", "box": "range", "reclaim": "push",
                            "under": "below", "wake": "reassess", "business": "trade",
                            "unchanged": "same", "framework": "thesis", "lean": "consider",
                            "actionable": "tradeable", "acceptance": "confirmation",
                            "continuation": "extension", "opens": "targets",
                            "stay": "remain", "flat": "idle", "decisive": "clear",
                            "engage": "enter", "respect": "watch", "especially": "particularly"}
                    _STOP = {"a", "an", "the", "is", "in", "on", "of", "to", "for",
                             "and", "or", "but", "not", "this", "that", "with", "from",
                             "at", "by", "do", "if", "it", "my", "no", "so", "be", "i"}
                    def _sm_norm(s):
                        s = s.lower().strip()
                        s = _re_sm.sub(r'\d{4,}\.?\d*', 'PRICE', s)
                        s = _re_sm.sub(r'[.,;:!?()"\'\-/]', ' ', s)
                        s = _re_sm.sub(r'\s+', ' ', s)
                        words = [_SYN.get(w, w) for w in s.split() if w not in _STOP and len(w) > 1]
                        return ' '.join(words)
                    _new_norm = _sm_norm(note_s)[:120]
                    _new_words = set(_new_norm.split())
                    for _existing_n in (payload.get("notes") or []):
                        _ex_text = _existing_n.get("note", _existing_n.get("text", "")) if isinstance(_existing_n, dict) else str(_existing_n)
                        if isinstance(_existing_n, dict) and str(_existing_n.get("source") or "").lower() == "sage":
                            continue
                        _ex_norm = _sm_norm(_ex_text)[:120]
                        _ex_words = set(_ex_norm.split())
                        if _new_words and _ex_words:
                            _overlap = len(_new_words & _ex_words) / max(len(_new_words), len(_ex_words))
                            if _overlap >= 0.55:
                                self._log_tool("write_session_memory", start, "REJECTED (similar note exists)")
                                _rej_preview = []
                                try:
                                    for _rn in (payload.get("notes") or [])[-5:]:
                                        _rnt = _rn.get("note", _rn.get("text", "")) if isinstance(_rn, dict) else str(_rn)
                                        _rej_preview.append(_rnt[:80])
                                except Exception:
                                    pass
                                return {
                                    "saved": False,
                                    "reason": "You already have a similar note in your memory. "
                                              "Are you seeing the market the same way, or are you missing something new? "
                                              "Look again at what price is actually doing right now.",
                                    "your_recent_notes": _rej_preview,
                                }
                except Exception:
                    pass

                payload["notes"].append({"time": utc_now().strftime("%H:%M"), "note": note_s})  # FLO-309 regression fix

                # FLO-241: Cap at 8 notes (was 20). Protect Sage notes.
                try:
                    all_notes = payload.get("notes") or []
                    sage_notes = [n for n in all_notes if isinstance(n, dict) and str(n.get("source") or "").lower() == "sage"]
                    normal_notes = [n for n in all_notes if not (isinstance(n, dict) and str(n.get("source") or "").lower() == "sage")]
                    normal_notes = normal_notes[-7:]  # 7 normal + sage notes = ~8 total
                    payload["notes"] = normal_notes + sage_notes
                    payload["notes"] = payload["notes"][-8:]
                except Exception:
                    payload["notes"] = payload["notes"][-8:]

            # Bug B commit 2: overwrite counters with fresh SQL values before
            # write. Merge at ~3628 may have imported stale 0/0/0 from the
            # existing file; this ensures Floki sees today's actual trades.
            # Helper is silent-fallback (zeros on DB error), never raises.
            try:
                from agent_memory import _read_daily_counters_for_session_date
                _counters = _read_daily_counters_for_session_date(str(payload.get("session_date") or ""))
                payload["trades_today"] = _counters["trades_today"]
                payload["wins_today"]   = _counters["wins_today"]
                payload["losses_today"] = _counters["losses_today"]
            except Exception:
                pass

            payload["last_updated"] = utc_iso()  # FLO-309 regression fix

            try:
                with open(mem_path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
            except Exception:
                self._log_tool("write_session_memory", start, "error=write_failed")
                return {"success": False, "reason": "write failed"}

            # Return existing notes so Floki sees what he already wrote
            _existing_preview = []
            try:
                for _n in (payload.get("notes") or [])[-5:]:
                    _nt = _n.get("note", _n.get("text", "")) if isinstance(_n, dict) else str(_n)
                    _existing_preview.append(_nt[:80])
            except Exception:
                pass

            self._log_tool("write_session_memory", start, f"notes_count={len(payload.get('notes') or [])}")
            return {
                "saved": True,
                "notes_count": len(payload.get("notes") or []),
                "your_recent_notes": _existing_preview,
                "reminder": "Review your notes above. Next time, only write what is genuinely NEW.",
            }
        except Exception as e:
            self._log_tool("write_session_memory", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    def _write_json_atomic(self, path: str, payload: Any) -> bool:
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            if os.path.exists(path):
                os.remove(path)
            os.rename(tmp, path)
            return True
        except Exception:
            return False

    def _watch_conditions_path(self) -> str:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        data_dir = os.path.join(base_dir, "data")
        os.makedirs(data_dir, exist_ok=True)
        return os.path.join(data_dir, "agent_watch_conditions.json")

    def _next_check_path(self) -> str:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        data_dir = os.path.join(base_dir, "data")
        os.makedirs(data_dir, exist_ok=True)
        return os.path.join(data_dir, "agent_next_check.json")

    def _wake_conditions_path(self) -> str:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        data_dir = os.path.join(base_dir, "data")
        os.makedirs(data_dir, exist_ok=True)
        return os.path.join(data_dir, "agent_wake_conditions.json")

    # ---------------------------------------------------------------------
    # Echo News Sentinel alerts
    # ---------------------------------------------------------------------

    def get_echo_alerts(self) -> Dict[str, Any]:
        """Read unread Echo alerts (IMPORTANT/CRITICAL). Marks as read."""
        start = time.time()
        try:
            from echo_sentinel import get_unread_alerts
            alerts = get_unread_alerts()
            elapsed = round((time.time() - start) * 1000, 1)
            if not alerts:
                return {"success": True, "alerts": [], "count": 0, "latency_ms": elapsed}
            return {
                "success": True,
                "alerts": alerts,
                "count": len(alerts),
                "latency_ms": elapsed,
            }
        except Exception as e:
            elapsed = round((time.time() - start) * 1000, 1)
            return {"success": False, "reason": f"echo_alerts_error: {e}", "latency_ms": elapsed}

    # ---------------------------------------------------------------------
    # Luna Macro Analyst brief
    # ---------------------------------------------------------------------

    def get_analyst_research(self) -> Dict[str, Any]:
        """FLO-419 Phase 2: Floki-specific Google-grounded analyst research for
        plan-building. Returns key support/resistance levels, intraday TA setups,
        and analyst directional bias for TODAY. Distinct from get_luna_brief
        (macro narrative) — this answers "what levels should I build plans around?"

        Cache TTL 30 min. First call per cycle pays ~3-8s latency for the search;
        subsequent calls within the TTL return instantly from disk. Empty arrays
        are returned when no concrete numeric levels are found rather than
        fabrications. Returns {"available": False, "reason": ...} on hard failure
        (no API key, network error, parse failure) so Floki sees the absence
        explicitly instead of silently missing the data."""
        start = time.time()
        try:
            from floki_research import get_floki_research
            data = get_floki_research()
            if not data:
                self._log_tool("get_analyst_research", start, "result=unavailable")
                return {
                    "available": False,
                    "reason": "research unavailable (API key missing, network error, or parse failure)",
                }
            self._log_tool(
                "get_analyst_research",
                start,
                (
                    f"sup={len(data.get('key_levels', {}).get('support', []))} "
                    f"res={len(data.get('key_levels', {}).get('resistance', []))} "
                    f"setups={len(data.get('setups_called_out', []))} "
                    f"bias={data.get('analyst_targets', {}).get('consensus_bias', '?')}"
                ),
            )
            return {
                "available": True,
                "timestamp": data.get("timestamp"),
                "key_levels": data.get("key_levels", {"support": [], "resistance": []}),
                "setups_called_out": data.get("setups_called_out", []),
                "analyst_targets": data.get("analyst_targets", {}),
                "key_themes_today": data.get("key_themes_today", []),
                "sources": data.get("sources", []),
            }
        except Exception as e:
            self._log_tool("get_analyst_research", start, f"error={type(e).__name__}:{e}")
            return {
                "available": False,
                "reason": f"tool error: {type(e).__name__}: {e}",
            }

    def get_market_regime(self) -> Dict[str, Any]:
        """
        FLO-290 commit 5: Local regime detector state.

        Reads data/regime_state.json (populated by regime_detector.py) and
        the in-memory _last_regime_context cached on the running bot. Returns
        the same fields the old <market_regime> auto-context block carried:
        regime, confidence, duration, stability, ADX, evidence, transition.

        This is the LOCAL XAU/USD regime (BREAKOUT_IMMINENT, TRANSITIONAL,
        QUIET, TRENDING_BULL, TRENDING_BEAR, RANGING, VOLATILE). Distinct
        from Luna's macro regime (risk_on/risk_off/crisis) available via
        get_luna_brief.
        """
        start = time.time()
        try:
            import json
            import os

            regime_ctx: Dict[str, Any] = {}
            _lrc = getattr(self._bot, "_last_regime_context", None)
            if isinstance(_lrc, dict):
                regime_ctx = dict(_lrc)

            history: list = []
            try:
                path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "regime_state.json")
                if os.path.exists(path):
                    with open(path, "r", encoding="utf-8") as f:
                        raw = json.load(f) or {}
                    history = raw.get("history", []) or []
                    if not regime_ctx.get("regime") and raw.get("regime"):
                        regime_ctx["regime"] = raw.get("regime")
                    regime_ctx.setdefault("change_ts", raw.get("change_ts"))
            except Exception:
                pass

            if not regime_ctx.get("regime"):
                self._log_tool("get_market_regime", start, "empty")
                return {"success": True, "regime": None, "reason": "no_regime_state_yet"}

            regime_changes_24h = 0
            try:
                cutoff = time.time() - 86400
                regime_changes_24h = sum(1 for h in history if (h.get("ts") or 0) >= cutoff)
            except Exception:
                pass

            _rn = regime_ctx.get("regime")
            _h4_bias = (regime_ctx.get("h4_volume_bias") or {}).get("bias")
            _macro_div = getattr(self._bot, "_last_macro_divergence", None)
            _rpd = regime_ctx.get("regime_price_divergence")
            _rpd_key = (_rpd or {}).get("price_direction") if _rpd else None
            _current_key = (
                _rn,
                regime_ctx.get("confidence", "moderate"),
                _h4_bias,
                (_macro_div or {}).get("signal"),
                _rpd_key,
            )
            if _current_key == getattr(self, "_last_regime_key", None) and _rn is not None:
                compact = {
                    "success": True,
                    "changed": False,
                    "regime": _rn,
                    "since": regime_ctx.get("duration_display", regime_ctx.get("duration", "?")),
                }
                self._log_tool("get_market_regime", start, f"regime={_rn} delta=unchanged")
                return compact

            # FLO-298: hint_map keys must match the regime-name strings emitted
            # by regime_detector (TRENDING_BULLISH/BEARISH, not TRENDING_BULL/BEAR).
            # The prior keys produced an empty base hint for the two trending regimes.
            hint_map = {
                "TRENDING_BULLISH": "Directional bias upward. Momentum indicators aligned to the upside over the regime duration.",
                "TRENDING_BEARISH": "Directional bias downward. Momentum indicators aligned to the downside over the regime duration.",
                "RANGING": "No sustained directional bias detected. Price oscillating within a band.",
                "VOLATILE": "Elevated ATR relative to recent baseline. Candle ranges expanded.",
                "BREAKOUT_IMMINENT": "ATR compressed relative to recent baseline. Range contracting over the regime duration.",
                "TRANSITIONAL": "Regime classification unstable. Indicators crossing thresholds between states.",
                "QUIET": "Below-average volume and ATR. Narrow candle ranges.",
            }

            # FLO-419 (CEO directive 2026-05-01, softened revision): the
            # prior FLO-298 hint ("price action and volume are more
            # reliable than regime labels") gave Floki license to override
            # the classifier with vibes — exactly what happened on
            # PLAN-20260501-022. A first revision blocked trend and
            # counter-trend plans entirely in high-turnover conditions,
            # but volatile regimes are when CEO most wants coverage.
            # Goal: QUALITY plans not FEWER plans. The hint guides
            # confidence + risk sizing for the uncertainty rather than
            # restricting setup choice.
            _hint = hint_map.get(_rn, "")
            if regime_changes_24h > 20:
                _hint = (
                    (_hint + " ") if _hint else ""
                ) + (
                    f"High regime turnover ({regime_changes_24h} changes/24h) — "
                    "regime labels are unreliable. Prioritize breakout and "
                    "range setups with clear invalidation levels. If authoring "
                    "trend-continuation or counter-trend plans, use lower "
                    "confidence (cap 60%) and tighter stop losses — the "
                    "regime may flip during the plan's lifetime."
                )

            payload = {
                "success": True,
                "regime": _rn,
                "confidence": regime_ctx.get("confidence", "moderate"),
                "duration_display": regime_ctx.get("duration_display", regime_ctx.get("duration", "?")),
                "previous_regime": regime_ctx.get("previous_regime", "?"),
                "stability": regime_ctx.get("stability", "?"),
                "regime_changes_24h": regime_changes_24h,
                "adx_current": regime_ctx.get("adx_current"),
                "atr_current": regime_ctx.get("atr_current"),
                "atr_ratio": regime_ctx.get("atr_ratio"),
                "transition": regime_ctx.get("transition"),
                "evidence": (regime_ctx.get("evidence") or [])[:5],
                "related_tools": ["get_chart_patterns"] if _rn in ("RANGING", "BREAKOUT_IMMINENT") else [],
                "hint": _hint,
                "h4_volume_bias": regime_ctx.get("h4_volume_bias"),
                "macro_divergence": _macro_div,
                "m15_explosive": regime_ctx.get("m15_explosive"),
                "regime_price_divergence": _rpd,
            }
            self._last_regime_key = _current_key
            self._log_tool(
                "get_market_regime",
                start,
                f"regime={_rn} stability={payload['stability']} changes24h={regime_changes_24h}",
            )
            return payload
        except Exception as e:
            self._log_tool("get_market_regime", start, f"error={e}")
            return {"success": False, "reason": f"market_regime_error: {e}"}

    def get_dxy_status(self) -> Dict[str, Any]:
        """
        FLO-432: DXY (Dollar Index) snapshot for gold-vs-USD reasoning.

        Returns current price, 1-day return %, 5-day return %, 30-day
        correlation with gold (XAUUSD), and a coarse signal label
        (DXY_RISING / DXY_FALLING / DXY_NEUTRAL based on 5-day return).

        DXY rising is historically headwind for gold (typical 30-day
        correlation -0.85 to -0.97); DXY falling is tailwind. CEO
        decision 2026-05-17: route this data directly to Floki rather
        than through Luna's macro brief so the trade decisor sees the
        primary inverse-correlate without intermediation.

        5-minute in-memory cache. Yahoo Finance (yfinance). Network
        failure or insufficient history returns a degraded payload with
        signal=DXY_UNKNOWN and an error field; never raises.
        """
        start = time.time()
        try:
            payload = _fetch_dxy_status_cached()
            self._log_tool(
                "get_dxy_status",
                start,
                f"signal={payload.get('signal')} 5d={payload.get('return_5d_pct')} "
                f"corr30={payload.get('correlation_30d')}",
            )
            return payload
        except Exception as e:
            self._log_tool("get_dxy_status", start, f"error={e}")
            return {
                "success": False,
                "signal": "DXY_UNKNOWN",
                "error": f"dxy_status_error: {type(e).__name__}: {e}",
            }

    def get_fair_value_gaps(self) -> Dict[str, Any]:
        """
        FLO-438 — detect unfilled Fair Value Gaps (FVGs) on H4 and H1.

        3-candle FVG rule:
          Bullish FVG: candle[i].high < candle[i+2].low (gap up — the
                       region [candle[i].high, candle[i+2].low] was
                       never traded).
          Bearish FVG: candle[i].low > candle[i+2].high (gap down).

        Unfilled = no subsequent candle has retraced the gap by ≥ 50%.
        For each TF, scan the last 100 candles and return up to 10 most
        recent unfilled FVGs (newest first).

        Output per FVG:
          direction, top, bottom, midpoint, size_pips, timeframe,
          age_candles, filled_pct, formed_at_iso

        FVGs are an ICT-framework concept — gaps left by displacement
        candles. Price tends to return to them because the institutional
        orders that drove the displacement are partially filled inside
        the gap. Use as entry zones, not just indicator readings.

        Read-only. Per-cycle MT5 fetch; no caching beyond the call.
        """
        start = time.time()
        try:
            result: Dict[str, Any] = {"H4": [], "H1": []}
            for tf_label, tf_const in (("H4", _mt5_tf("H4")), ("H1", _mt5_tf("H1"))):
                if tf_const is None:
                    continue
                fvgs = _scan_fvgs(tf_label, tf_const, lookback=100, max_results=10)
                result[tf_label] = fvgs
            count = sum(len(v) for v in result.values())
            self._log_tool("get_fair_value_gaps", start, f"unfilled_count={count}")
            return {"success": True, "fvgs": result, "count": count}
        except Exception as e:
            self._log_tool("get_fair_value_gaps", start, f"error={e}")
            return {"success": False, "error": f"fvg_error: {type(e).__name__}: {e}"}

    def get_liquidity_sweeps(self) -> Dict[str, Any]:
        """
        FLO-438 — detect recent liquidity sweeps on H4 and H1.

        A sweep is a wick that pierces a prior swing high/low and closes
        back inside — the classic stop-hunt pattern. The market grabs
        liquidity (resting stops above/below the swing) then reverses.

        Detection:
          1. Identify swing highs/lows over `lookback` candles using a
             fractal: a candle is a swing high if its high is the max
             over [i-fractal_window, i+fractal_window] (and similarly
             for swing lows). Default fractal_window=3.
          2. For each candle AFTER the swing, check if its wick pierced
             the swing level but its close is back inside.
          3. Report up to 10 most recent sweeps per timeframe.

        Output per sweep:
          level, direction (BSL/SSL), sweep_candle_time_iso,
          wick_size_pips, recovered_pct, timeframe

        Gold sweeps the Asian-range high/low almost every London/NY
        session — the "first break is often not the truth." Sweeps are
        the most common reversal precursor; entering inside the sweep
        wick after a confirmation MSS is a textbook ICT entry.

        Read-only. Per-cycle MT5 fetch; no caching beyond the call.
        """
        start = time.time()
        try:
            result: Dict[str, Any] = {"H4": [], "H1": []}
            for tf_label, tf_const in (("H4", _mt5_tf("H4")), ("H1", _mt5_tf("H1"))):
                if tf_const is None:
                    continue
                sweeps = _scan_sweeps(tf_label, tf_const, lookback=100, max_results=10)
                result[tf_label] = sweeps
            count = sum(len(v) for v in result.values())
            self._log_tool("get_liquidity_sweeps", start, f"sweep_count={count}")
            return {"success": True, "sweeps": result, "count": count}
        except Exception as e:
            self._log_tool("get_liquidity_sweeps", start, f"error={e}")
            return {"success": False, "error": f"sweep_error: {type(e).__name__}: {e}"}

    def get_chart_patterns(self) -> Dict[str, Any]:
        """
        FLO-290 commit 5: Algorithmic H4 pattern detection.

        Detects double top/bottom, head & shoulders, failed breakouts, rising
        and falling wedges, channels from the last 30 H4 bars. Same logic
        that previously injected into <market_structure>.PATTERNS — now
        Floki-queryable.

        Returns a list of pattern dicts with type, bias, price level, and a
        human-readable description.
        """
        start = time.time()
        try:
            from mt5_safe import mt5 as _mt5  # FLO-348
            from pattern_detector import detect_patterns

            bars = _mt5.copy_rates_from_pos("XAUUSD", _mt5.TIMEFRAME_H4, 0, 30)
            current_price: Optional[float] = None
            try:
                tick = _mt5.symbol_info_tick("XAUUSD")
                if tick is not None:
                    current_price = float(getattr(tick, "last", 0) or getattr(tick, "bid", 0) or 0)
            except Exception:
                pass
            if not current_price:
                current_price = float(getattr(self._bot, "last_known_price", 0) or 0)
            if not current_price:
                self._log_tool("get_chart_patterns", start, "no_price")
                return {"success": False, "reason": "price_unavailable"}

            patterns = detect_patterns(bars, current_price)
            self._log_tool(
                "get_chart_patterns",
                start,
                f"patterns={len(patterns)} price={round(current_price, 2)}",
            )
            return {
                "success": True,
                "timeframe": "H4",
                "bars_analyzed": 30,
                "current_price": round(float(current_price), 2),
                "count": len(patterns),
                "patterns": patterns,
            }
        except Exception as e:
            self._log_tool("get_chart_patterns", start, f"error={e}")
            return {"success": False, "reason": f"chart_patterns_error: {e}"}

    def get_luna_brief(self) -> Dict[str, Any]:
        """Read the latest Luna macro analysis brief."""
        start = time.time()
        try:
            from luna_analyst import load_luna_brief
            brief = load_luna_brief()
            elapsed = round((time.time() - start) * 1000, 1)

            if brief is None:
                return {"success": True, "brief": None, "stale": True, "latency_ms": elapsed}

            # Check freshness — flag if older than 30 min
            stale = False
            ts = brief.get("timestamp")
            if ts:
                try:
                    from datetime import datetime, timezone
                    brief_time = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    age_min = (datetime.now(timezone.utc) - brief_time).total_seconds() / 60
                    stale = age_min > 30
                    brief["age_minutes"] = round(age_min, 1)
                except Exception:
                    pass

            return {
                "success": True,
                "brief": brief,
                "stale": stale,
                "latency_ms": elapsed,
            }
        except Exception as e:
            elapsed = round((time.time() - start) * 1000, 1)
            return {"success": False, "reason": f"luna_brief_error: {e}", "latency_ms": elapsed}

    def get_rex_monitor(self) -> Dict[str, Any]:
        """Read latest Rex proactive monitoring scan (FLO-211)."""
        start = time.time()
        try:
            from rex_monitor import load_rex_monitor
            monitor = load_rex_monitor()
            elapsed = round((time.time() - start) * 1000, 1)

            if monitor is None:
                self._log_tool("get_rex_monitor", start, "empty/stale")
                return {"success": True, "monitor": None, "stale": True, "latency_ms": elapsed}

            stale = False
            age_minutes = None
            ts = monitor.get("timestamp")
            if ts:
                try:
                    from datetime import datetime, timezone
                    scan_time = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    age_minutes = round((datetime.now(timezone.utc) - scan_time).total_seconds() / 60, 1)
                    stale = age_minutes > 60  # FLO-313: 2× interval (was 30 = 1× interval)
                except Exception:
                    pass

            # FLO-316: alert_level / alert_context / alert_hint removed from
            # Rex Monitor output (prescriptive labels violated Escola 1).
            # Surface observational summary only: findings_count + findings[]
            # where each finding is {type, observation, data}.
            summary = {
                "findings_count": monitor.get("findings_count", monitor.get("finding_count", 0)),
                "findings": monitor.get("findings", []),
                "timestamp": ts,
            }

            # Refresh regime duration in findings (frozen at scan time)
            try:
                import re as _re
                regime_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "regime_state.json")
                if os.path.exists(regime_path):
                    import json as _json2
                    with open(regime_path, "r", encoding="utf-8") as _rf:
                        _rdata = _json2.load(_rf)
                    _cts = _rdata.get("change_ts")
                    if _cts:
                        _fresh_dur = int((time.time() - float(_cts)) / 60)
                        for _f in summary.get("findings", []):
                            if _f.get("type") == "REGIME_CHANGE":
                                _f["detail"] = _re.sub(r'\d+m ago$', f'{_fresh_dur}m ago', _f["detail"])
            except Exception:
                pass

            self._log_tool("get_rex_monitor", start, f"findings={summary['findings_count']}")
            return {
                "success": True,
                "monitor": summary,
                "stale": stale,
                "age_minutes": age_minutes,
                "latency_ms": elapsed,
            }
        except Exception as e:
            elapsed = round((time.time() - start) * 1000, 1)
            self._log_tool("get_rex_monitor", start, f"error={e}")
            return {"success": False, "reason": f"rex_monitor_error: {e}", "latency_ms": elapsed}

    # FLO-262: Available timeframes for chart screenshots
    _CHART_TFS = ["H4", "H1", "M15"]  # FLO-454: cut from 6 (was D1/H4/H1/M15/M5/M1)
    # to 3 — H4 bias / H1 setup / M15 entry. Fewer, cleaner visual inputs (Chroma
    # Context-Rot: vision accuracy degrades with more panels). Indicator-panel
    # cleanup (MACD/BB/Stoch off) is a MANUAL MT5 chart-template change — the EA
    # ChartScreenShot()s whatever the terminal template shows; not code-settable.

    def get_chart_screenshots(self, timeframes: list = None) -> Dict[str, Any]:
        """Return chart screenshots for requested timeframes. Images injected by caller.

        FLO-262: Accepts optional timeframes list (e.g. ['M5'], ['H4','D1']).
        If omitted, returns all available timeframes.
        """
        start = time.time()
        ci = getattr(self, '_chart_images', {}) or {}

        # Determine which TFs to return
        if timeframes and isinstance(timeframes, list):
            requested = [tf.upper().strip() for tf in timeframes if isinstance(tf, str)]
            requested = [tf for tf in requested if tf in self._CHART_TFS]
        else:
            requested = list(self._CHART_TFS)  # all available

        available = {}
        for tf in requested:
            key = f"{tf.lower()}_b64"
            if ci.get(key):
                available[tf] = len(ci[key]) // 1024  # KB

        if not available:
            self._log_tool("get_chart_screenshots", start, f"no screenshots for {requested}")
            return {"success": False, "reason": f"No screenshots available for {requested}"}

        parts = [f"{tf}({kb}KB)" for tf, kb in available.items()]
        # FLO-322: surface REQUESTED TFs in the log line too, not just the
        # returned ones. Makes "which TFs did Floki ask for" queryable from
        # log grep even when tool_trace isn't available. Tool_trace already
        # captures the full input args dict in the 'input' field.
        req_str = ",".join(requested) if requested else "all"
        self._log_tool(
            "get_chart_screenshots", start,
            f"requested={req_str} | returning {' '.join(parts)}"
        )

        result = {"success": True, "timeframes": list(available.keys())}
        for tf in available:
            result[tf.lower()] = True
        result["note"] = f"Charts attached: {', '.join(available.keys())}. Analyze candle patterns, S/R zone interactions, volume bars, and momentum visually."
        return result

    # ================================================================
    # PENDING ORDERS (FLO-263)
    # ================================================================

    def place_pending_order(self, order_type: str, price: float, sl: float, tp: float,
                            expiry_minutes: int = 60, reason: str = "",
                            override_duplicate: bool = False) -> Dict[str, Any]:
        """Place a pending order (BUY_LIMIT/SELL_LIMIT/BUY_STOP/SELL_STOP).

        FLO-316: refuses placement when an existing pending order of the SAME
        type sits within 50 pips of the requested price, unless
        override_duplicate=True. Match is on type + price only — lot size and
        SL/TP are ignored because different lots/stops at the same level can
        be intentional scaling. Opposite-direction pendings at the same price
        are valid brackets and never trigger the warning.
        """
        start = time.time()
        import config

        if not getattr(config, "PENDING_ORDERS_ENABLED", False):
            self._log_tool("place_pending_order", start, "DISABLED")
            return {"success": False, "reason": "Pending orders disabled"}

        valid = ("BUY_LIMIT", "SELL_LIMIT", "BUY_STOP", "SELL_STOP")
        ot = str(order_type or "").upper().strip()
        if ot not in valid:
            self._log_tool("place_pending_order", start, f"invalid type: {ot}")
            return {"success": False, "reason": f"Invalid type. Use: {', '.join(valid)}"}

        try:
            price_f, sl_f, tp_f = float(price), float(sl), float(tp)
        except Exception:
            return {"success": False, "reason": "Invalid price/sl/tp"}

        # FLO-316: duplicate pending order detection. Pre-flight check BEFORE
        # the safety/sizing/MT5 path, so we don't waste any work when Floki
        # is about to stack a second identical order on an existing one.
        # Always runs — override_duplicate just decides whether a match
        # refuses placement (False) or logs an audit notice and proceeds (True).
        dup_found = None
        try:
            existing = self._executor.get_pending_orders() or []
            DUPLICATE_PIP_WINDOW = 50.0  # XAU pip = 0.1 → 5.0 price units
            window = DUPLICATE_PIP_WINDOW * 0.1
            for o in existing:
                if str(o.get("type") or "").upper() == ot:
                    try:
                        ep = float(o.get("price"))
                    except Exception:
                        continue
                    if abs(ep - price_f) <= window:
                        dup_found = o
                        break
        except Exception:
            # Fire-and-forget — never block a legitimate placement on a
            # check that itself errored. Same philosophy as other
            # defensive reads throughout this module.
            dup_found = None

        if dup_found and not override_duplicate:
            diff_pips = round(abs(float(dup_found["price"]) - price_f) / 0.1, 1)
            msg = (
                f"You already have a pending {ot} @ {dup_found['price']} "
                f"(ticket #{dup_found['ticket']}). Requested {ot} @ {price_f} "
                f"is {diff_pips} pips away. If intentional (e.g., "
                f"bracket sizing), re-call with override_duplicate=true."
            )
            self._log_tool(
                "place_pending_order", start,
                f"DUPLICATE_DETECTED | existing #{dup_found['ticket']} @ {dup_found['price']} | requested @ {price_f}"
            )
            return {
                "success": False,
                "reason": "duplicate_pending_order",
                "existing_ticket": dup_found["ticket"],
                "existing_price": dup_found["price"],
                "existing_type": ot,
                "requested_price": price_f,
                "price_diff_pips": diff_pips,
                "warning": msg,
            }
        if dup_found and override_duplicate:
            # Audit trail — Floki explicitly overrode the warning. Useful
            # for post-hoc review of intentional bracket stacking.
            log.info(
                f"PENDING_ORDER | DUPLICATE_OVERRIDE | placing {ot} @ {price_f} "
                f"alongside existing #{dup_found['ticket']} @ {dup_found['price']}"
            )

        dir_s = "BUY" if "BUY" in ot else "SELL"
        sl_pips = abs(price_f - sl_f) / 0.1

        # Safety checks (same as execute_trade)
        acct = self._executor.get_account_info() or {}
        balance = self._safe_float(acct.get("balance"))
        if not balance or balance <= 0:
            self._log_tool("place_pending_order", start, "REJECTED | account balance unavailable")
            return {"success": False, "reason": "account balance unavailable"}

        try:
            open_positions_list = self._executor.get_open_positions() or []
        except Exception:
            open_positions_list = []

        is_safe, reasons = self._safety.is_safe_to_trade(
            account_balance=float(balance),
            open_positions=len(open_positions_list),
            mt5_connected=True,
            has_high_impact_news=False,
            trade_direction=dir_s,
            open_positions_list=open_positions_list,
        )
        if not is_safe:
            self._log_tool("place_pending_order", start, f"REJECTED | safety: {'; '.join(reasons[:3])}")
            return {"success": False, "reason": "; ".join(reasons[:3])}

        # Risk sizing
        risk_pct = float(getattr(config, "RISK_PER_TRADE", 2.0))
        pos = self._risk.calculate_position_size(
            account_balance=float(balance),
            risk_percent=risk_pct,
            stop_loss_pips=float(sl_pips),
        )

        exp = max(1, int(expiry_minutes)) if expiry_minutes else 60
        res = self._executor.place_pending_order(
            order_type_str=ot,
            price=price_f,
            lot_size=float(pos.lot_size),
            stop_loss=sl_f,
            take_profit=tp_f,
            expiry_minutes=exp,
            comment=f"Pending-{ot}",
        )

        if res.get("success"):
            ticket = res.get("ticket")

            # FLO-269: Record pending order in trades table (ticket=0 placeholder).
            # monitor.update_trade_open_price() updates to real ticket on fill.
            try:
                from db_writer import record_trade_open
                record_trade_open(
                    ticket=0,
                    direction=dir_s,
                    volume=float(pos.lot_size),
                    open_price=price_f,
                    sl=sl_f,
                    tp=tp_f,
                    comment=f"Pending-{ot}",
                    decision_source="floki_agent",
                )
            except Exception:
                pass

            self._log_tool("place_pending_order", start,
                f"{ot} @ {price_f} SL={sl_f} TP={tp_f} lot={pos.lot_size} exp={exp}min ticket={ticket}")
            return {"success": True, "ticket": ticket, "type": ot, "price": price_f,
                    "sl": sl_f, "tp": tp_f, "volume": float(pos.lot_size), "expiry_minutes": exp}
        else:
            self._log_tool("place_pending_order", start, f"FAILED | {res.get('error')}")
            return {"success": False, "reason": res.get("error", "placement failed")}

    def cancel_pending_order(self, ticket: int = None, cancel_all: bool = False) -> Dict[str, Any]:
        """Cancel a pending order by ticket, or cancel all pending orders.

        FLO-317: after a successful cancel, purges the associated ticket=0
        placeholder row(s) in the trades table. Otherwise cancelled orders
        leave stale rows until the next place_pending_order call triggers
        the FLO-308 pre-INSERT purge.
        """
        start = time.time()
        if cancel_all:
            res = self._executor.cancel_all_pending()
            if res.get("success"):
                try:
                    from db_writer import purge_unfilled_placeholders
                    _n = purge_unfilled_placeholders()
                    if _n > 0:
                        log.info(f"PENDING_CANCEL | cancel_all purged {_n} ticket=0 placeholder(s)")
                except Exception:
                    pass
            self._log_tool("cancel_pending_order", start, f"cancel_all | cancelled={res.get('cancelled', 0)}")
            return res
        if not ticket:
            return {"success": False, "reason": "ticket required (or cancel_all=true)"}
        res = self._executor.cancel_pending_order(int(ticket))
        if res.get("success"):
            try:
                from db_writer import purge_unfilled_placeholders
                _n = purge_unfilled_placeholders()
                if _n > 0:
                    log.info(f"PENDING_CANCEL | ticket={ticket} purged {_n} ticket=0 placeholder(s)")
            except Exception:
                pass
        self._log_tool("cancel_pending_order", start, f"ticket={ticket} | success={res.get('success')}")
        return res

    def get_pending_orders(self) -> Dict[str, Any]:
        """List all current pending orders."""
        start = time.time()
        orders = self._executor.get_pending_orders()
        self._log_tool("get_pending_orders", start, f"count={len(orders)}")
        return {"success": True, "orders": orders, "count": len(orders)}

    def write_trading_journal(self, entry: str, category: str = "reflection") -> Dict[str, Any]:
        """Append an entry to Floki's persistent trading journal."""
        start = time.time()
        try:
            entry_s = str(entry or "").strip()
            if not entry_s:
                return {"success": False, "reason": "empty entry"}

            cat_s = str(category or "reflection").strip().lower()
            valid_cats = ("reflection", "missing_data", "lesson", "frustration", "idea", "market_observation")
            if cat_s not in valid_cats:
                cat_s = "reflection"

            base_dir = os.path.dirname(os.path.abspath(__file__))
            journal_path = os.path.join(base_dir, "data", "floki_journal.json")
            os.makedirs(os.path.dirname(journal_path), exist_ok=True)

            entries: list = []
            if os.path.exists(journal_path):
                try:
                    with open(journal_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, list):
                        entries = data
                except Exception:
                    entries = []

            entries.append({
                "timestamp": utc_iso(),  # FLO-309
                "category": cat_s,
                "entry": entry_s,
            })

            tmp_path = journal_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(entries, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, journal_path)

            self._log_tool("write_trading_journal", start, f"cat={cat_s} len={len(entry_s)}")
            return {"success": True, "total_entries": len(entries)}
        except Exception as e:
            self._log_tool("write_trading_journal", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    # ---------------------------------------------------------------------
    # Snow plan-management tools (FLO-347 Phase 6)
    #
    # IMPORTANT — BEHAVIOUR DURING OBSERVATION WINDOW:
    # These tools ship BEFORE Floki's system prompt is updated to describe
    # Snow. The 4 methods below are fully functional and tested, but Floki
    # has no prompt-level guidance telling him when to call them, so in
    # practice he will not invoke them. This is intentional — we want the
    # mechanics shipped, tested, and auditable before the prompt change
    # flips Floki's behaviour. Phase 6.5/7 updates the prompt and starts
    # the evidence window. If you see an empty `snow_plans` table after
    # restart, that is expected until then, not a bug.
    # ---------------------------------------------------------------------

    # FLO-422 Phase A1 (2026-05-07): get_snow_primitives_reference removed.
    # Qwen-era scaffolding. Vocabulary lives in agent_prompts.py condition
    # primitives section (line ~321) and validator errors carry the closed
    # list inline. Module snow/reference.py also removed in same commit.

    def get_snow_recipe_book(
        self, category: Optional[str] = None,
    ) -> Dict[str, Any]:
        """FLO-358 — return curated multi-indicator setup recipes.

        The recipe book is the inspirational layer of the management /
        confluence vocabulary. Each recipe combines two or more Snow
        Condition primitives into a setup pattern drawn from
        established TA methodology (CMT body, classical chart
        patterns, candlestick literature, regime-based confluence).
        Recipes describe how traders historically frame setups —
        descriptive voice, NOT prescriptive directives. You retain
        full agency over plan composition.

        Args:
          category: Filter to one of "trend", "range", "reversal",
            "risk_management". None returns all recipes.

        Returns:
          {"success": True, "version": str, "source_note": str,
           "category_filter": str|None, "count": int,
           "recipes": [{id, title, category, primary_signal,
                        setup_type_alignment, common_ingredients,
                        when_traders_favor_it, what_it_captures,
                        variations, framing_note}, ...]}
          {"success": False, "reason": "..."}

        Source of truth: `data/_design/snow_recipe_book.md` parsed
        by `snow.recipe_book.load_recipe_book` (cached by mtime).
        """
        start = time.time()
        try:
            from snow.recipe_book import get_recipes_by_category
        except Exception as e:
            self._log_fail(
                "get_snow_recipe_book", start, f"import_error={e}",
            )
            return {
                "success": False,
                "reason": f"snow.recipe_book import failed: {e}",
            }
        try:
            result = get_recipes_by_category(category=category)
            count = result.get("count", 0)
            # FLO-382 D1: track the pull for the current cycle so
            # submit_plan_to_snow can emit recipe-adoption telemetry.
            try:
                self._recipe_pulls.append({
                    "ts": utc_iso(),
                    "category": category,
                    "count": int(count),
                })
            except Exception:
                pass
            # FLO-393: per-cycle counter for the hard gate in
            # submit_plan_to_snow. Increment is independent of the
            # FLO-382 deque so the gate works even if the deque
            # bookkeeping above raised.
            try:
                self._recipe_pulls_count += 1
            except Exception:
                pass
            self._log_tool(
                "get_snow_recipe_book", start,
                f"category={category!r} count={count}",
            )
            return result
        except FileNotFoundError as e:
            self._log_fail(
                "get_snow_recipe_book", start, f"source_missing={e}",
            )
            return {
                "success": False,
                "reason": f"recipe book source missing: {e}",
            }
        except Exception as e:
            self._log_fail("get_snow_recipe_book", start, f"error={e}")
            return {"success": False, "reason": f"{type(e).__name__}: {e}"}

    # FLO-422 Phase A1 (2026-05-07): get_snow_tags_reference removed.
    # Qwen-era scaffolding. Vocabulary lives in agent_prompts.py
    # (lines ~175-178: setup_type + context_tags closed lists) and
    # validator errors carry the full closed list inline. Module
    # snow/tags_reference.py also removed in same commit.

    def submit_plan_to_snow(
        self, plan: Optional[Dict[str, Any]] = None, **kwargs: Any,
    ) -> Dict[str, Any]:
        """Submit a contingency plan to Snow for autonomous monitoring.

        Snow evaluates the plan's conditions on a 5 s cadence and fires
        the associated actions when all-true. During DRY RUN mode
        (`config.SNOW_DRY_RUN=true`, default) fires are logged as
        `*_would_fire` events in `snow_evaluations` — NO real orders hit
        MT5. When `SNOW_DRY_RUN=false`, fires dispatch to the real
        executor under `executor_lock`.

        Argument shape — accepts BOTH:
          * Wrapped:  submit_plan_to_snow(plan={"analysis": ..., "entry": ...})
          * Direct:   submit_plan_to_snow(analysis=..., entry=..., ...)

        The wrapper-shape is the canonical OpenAI tool-call form. The
        direct shape is what Floki naturally produces when copying the
        prompt's MINIMAL PLAN EXAMPLE / EXPLORATORY SCENARIO EXAMPLE
        (which display the inner plan body without the `plan:` wrapper).
        Both normalize to the same internal plan dict — no semantic
        difference, no double-validation, no schema branching downstream.

        Returns:
          {"success": True,  "plan_id": "PLAN-YYYYMMDD-NNN",
           "validation_errors": None}
          {"success": False, "plan_id": None,
           "validation_errors": [str, ...]}
          {"success": False, "reason": "...internal error..."}

        The plan dict's `id`, `created_by`, and `created_at` fields are
        ALWAYS overwritten by the tool — Floki cannot spoof them. All
        other fields come from Floki.
        """
        start = time.time()
        try:
            from snow import db as _snow_db
            from snow.validator import validate_plan as _validate
        except Exception as e:
            self._log_tool("submit_plan_to_snow", start, f"import_error={e}")
            return {"success": False, "reason": f"snow import failed: {e}"}

        # Normalize both call shapes into a single `plan` dict before
        # any downstream logic. The decision rule:
        #   - If `plan` was passed and is a dict → use it (wrapper shape).
        #   - Else if kwargs contains plan-body keys → kwargs IS the plan.
        #   - Else → invalid input.
        # Edge: if BOTH `plan` (dict) and kwargs are present, the wrapper
        # wins; kwargs are ignored. This is a defensive choice — the
        # wrapper is the explicit canonical form, kwargs would only
        # appear here via an unusual call pattern.
        if plan is None and kwargs:
            plan = kwargs

        if not isinstance(plan, dict):
            self._log_tool("submit_plan_to_snow", start, "non_dict_input")
            return {
                "success": False, "plan_id": None,
                "validation_errors": ["plan must be a dict"],
            }

        # Defensive: if Floki double-wrapped (plan={"plan": {...}}),
        # unwrap once. Cheap normalization — protects against the LLM
        # over-correcting after seeing the wrapper-shape example.
        if (
            isinstance(plan.get("plan"), dict)
            and "analysis" in plan["plan"]
            and "analysis" not in plan
        ):
            plan = plan["plan"]

        # FLO-404 v3 (CEO directive 2026-04-30) — Layer B null-path defense.
        # Gemini's strict schema-follower tool generator has been observed
        # emitting `null` at list-of-object paths (entry.conditions[*],
        # management[*], exit[*]) and at analysis.context_tags. The
        # tightened input_schema in ai_agent.py (Layer A) steers Gemini
        # away from this; this is belt-and-suspenders that catches the
        # case if the schema steering ever slips. We surface a clear,
        # actionable error naming each null path so Floki's retry
        # round-trip (Maximum 3 attempts, FLO-393) corrects it.
        _null_paths = AgentTools._scan_null_object_paths(plan)
        if _null_paths:
            self._log_fail(
                "submit_plan_to_snow", start,
                f"null_at_object_paths={_null_paths}",
            )
            return {
                "success": False, "plan_id": None,
                "validation_errors": [
                    f"FLO-404: plan has `null` at {len(_null_paths)} "
                    f"path(s) where a populated object is required: "
                    f"{', '.join(_null_paths)}. Each of these paths must "
                    f"be a real dict — entry.conditions[*] are condition "
                    f"primitives ({{'type': '...', ...}}); management[*] "
                    f"and exit[*] are contingencies ({{'name', 'priority', "
                    f"'conditions', 'action', 'fires'}}); analysis."
                    f"context_tags is {{'trend', 'volatility', 'htf', "
                    f"'news_session'}}. Resubmit with these paths "
                    f"populated; the schema does not accept null at any "
                    f"of them."
                ],
            }

        # FLO-408 Phase 2 (CEO directive 2026-04-30) — Layer C missing-
        # required-field defense. Phase 1 corpus capture (data/_audits/
        # gemini_format_corpus_*) showed Gemini's tool generator omits
        # required fields entirely (12/17 corpus submits missing
        # analysis.thesis + analysis.confidence; 9/17 missing
        # entry.direction/volume/initial_sl/initial_tp; 3/17 missing
        # entry/exit blocks). The Layer A required-arrays in
        # ai_agent.py force the LLM tool generator to populate them at
        # generation time; this is belt-and-suspenders that catches
        # the case if any slip through (e.g. partial-submission-in-
        # batch where call #2's tool_call gets stripped to a delta).
        # Surfaces a single structured error naming every missing path
        # so Floki's 3-attempt retry budget corrects on attempt 2.
        _missing_fields = AgentTools._scan_missing_required_fields(plan)
        if _missing_fields:
            self._log_fail(
                "submit_plan_to_snow", start,
                f"missing_required_fields={_missing_fields}",
            )
            return {
                "success": False, "plan_id": None,
                "validation_errors": [
                    f"FLO-408: plan is missing {len(_missing_fields)} "
                    f"required field(s): {', '.join(_missing_fields)}. "
                    f"Every plan must have a complete analysis (thesis, "
                    f"key_levels, confidence, regime_assumed, setup_type, "
                    f"context_tags, confidence_reason), entry (direction, "
                    f"volume, conditions, initial_sl, initial_tp, "
                    f"entry_price), management items (name, priority, "
                    f"conditions, action, fires), exit items (same), "
                    f"emergency (max_loss_pips, max_duration_minutes, "
                    f"on_broker_error). If you batched multiple "
                    f"submit_plan_to_snow calls in one assistant turn, "
                    f"emit each in its own turn instead — some tool-call "
                    f"generators strip subsequent calls to deltas, "
                    f"which produces this missing-required-fields shape."
                ],
            }

        # FLO-393: mandatory Recipe Book consultation gate. Reject plans
        # submitted without at least one `get_snow_recipe_book` call
        # earlier in the same Floki cycle. Counter is reset to 0 at the
        # top of `agent_decide()` (canonical cycle start). Paired-hedge
        # cycles work fine: the first submit and the second submit in
        # the same cycle both see `count >= 1` because the counter
        # accumulates across the cycle and only resets on the next
        # `agent_decide()` invocation.
        if int(getattr(self, "_recipe_pulls_count", 0)) == 0:
            self._log_fail(
                "submit_plan_to_snow",
                start,
                "no_recipe_consultation recipe_pulls_count=0",
            )
            return {
                "success": False, "plan_id": None,
                "validation_errors": [
                    "FLO-393: plan submission requires at least one "
                    "get_snow_recipe_book call earlier in this cycle. "
                    "Recipe Book consultation is mandatory — call "
                    "get_snow_recipe_book(category=...) before "
                    "submit_plan_to_snow. Suggested categories per setup "
                    "type: trend / range / reversal / risk_management. "
                    "Pull whichever matches your thesis, then resubmit "
                    "this same plan dict — no plan-shape changes needed."
                ],
            }

        # Collision retry — two concurrent callers might compute the same
        # NNN before either has inserted. PRIMARY KEY rejects the second;
        # regenerate and retry up to 3 times. Under Floki's cadence this
        # is exceedingly unlikely to fire even once.
        import sqlite3 as _sql
        last_err: Optional[str] = None
        for attempt in range(1, 4):
            try:
                plan_id = _snow_db.generate_plan_id()
                candidate = dict(plan)
                candidate["id"] = plan_id
                candidate["created_by"] = "floki"  # schema Literal; locked
                candidate["created_at"] = utc_iso()
                # status is set by Plan schema default (PENDING); if the
                # caller passed something else, overwrite to be safe.
                candidate["status"] = "pending"

                # FLO-427: snapshot the live regime so the validator's
                # counter-trend gate can read it without re-computing.
                # Cheap dict-read from bot's in-memory state; fail-soft.
                _author_regime: Optional[Dict[str, Any]] = None
                _author_d1_trend: Optional[Dict[str, Any]] = None
                try:
                    _ctx = getattr(self._bot, "_last_regime_context", None)
                    if isinstance(_ctx, dict):
                        _author_regime = {
                            "regime": _ctx.get("regime"),
                            "confidence": _ctx.get("confidence"),
                            "adx": _ctx.get("adx"),
                            # FLO-430 — D1/H4 EMA50 alignment for the ADX
                            # override branch in _check_regime_counter_trend_gate.
                            "d1_direction": _ctx.get("d1_direction"),
                            "h4_direction": _ctx.get("h4_direction"),
                        }
                        # FLO-452 — D1 trend score for the D1_TREND_GATE.
                        _author_d1_trend = _ctx.get("d1_trend_score")
                except Exception:
                    _author_regime = None

                # FLO-452 wiring fix: _last_regime_context intermittently lacks
                # d1_trend_score (a cycle's build returned None at that moment),
                # which DEGRADED the gate on every real plan today. Fall back to
                # the score state_writer persisted to bot_state.json — the same
                # cheap-read pattern as _build_specialist_context's multi_tf read.
                if _author_d1_trend is None:
                    try:
                        _bs_path = os.path.join(
                            os.path.dirname(os.path.abspath(__file__)),
                            "data", "bot_state.json",
                        )
                        with open(_bs_path, "r", encoding="utf-8") as _bsf:
                            _author_d1_trend = json.load(_bsf).get("d1_trend_score")
                    except Exception:
                        _author_d1_trend = None

                # FLO-453 — H1 ADX + slope for the setup-regime matrix gate.
                # Cheap read from bot_state.json multi_tf_indicators.H1.
                _author_setup_ctx: Optional[Dict[str, Any]] = None
                try:
                    _bs_path2 = os.path.join(
                        os.path.dirname(os.path.abspath(__file__)),
                        "data", "bot_state.json",
                    )
                    with open(_bs_path2, "r", encoding="utf-8") as _bsf2:
                        _h1 = (json.load(_bsf2).get("multi_tf_indicators") or {}).get("H1") or {}
                    _adx_obj = _h1.get("adx")
                    _adx_val = _adx_obj.get("value") if isinstance(_adx_obj, dict) else _adx_obj
                    # rising: prefer 4-bar change sign, fall back to direction label.
                    _chg = _h1.get("adx_change_4bars")
                    if isinstance(_chg, (int, float)):
                        _rising = _chg > 0
                    else:
                        _rising = str(_h1.get("adx_direction", "")).lower() in ("rising", "up", "increasing")
                    if _adx_val is not None:
                        _author_setup_ctx = {"adx": _adx_val, "adx_rising": _rising}
                except Exception:
                    _author_setup_ctx = None

                # FLO-436 — pull last calendar snapshot for news-blackout gate.
                _author_calendar: Optional[list] = None
                try:
                    _cal = getattr(self._bot, "_last_calendar_data", None)
                    if isinstance(_cal, list):
                        _author_calendar = _cal
                    elif isinstance(_cal, dict):
                        _evs = _cal.get("events") or _cal.get("calendar")
                        if isinstance(_evs, list):
                            _author_calendar = _evs
                except Exception:
                    _author_calendar = None

                # FLO-439 — pull account balance + today's realized P&L for
                # the daily-loss-limit gate. Fail-soft: any error → None
                # and the gate logs DEGRADED + allows the plan.
                _author_account: Optional[Dict[str, Any]] = None
                try:
                    _ex = getattr(self, "_executor", None) or getattr(self._bot, "executor", None)
                    _ai = _ex.get_account_info() if _ex is not None else None
                    _balance = float(_ai.get("balance", 0)) if isinstance(_ai, dict) else 0.0
                    _pnl = _today_realized_pnl_usd()
                    _author_account = {
                        "balance": _balance,
                        "today_pnl_usd": _pnl,
                    }
                except Exception:
                    _author_account = None

                # FLO-443 — self-consistency voter (env-gated, default
                # OFF). When enabled, runs 5 parallel Sonnet votes on
                # the plan's analysis summary at temperature 0.7. The
                # majority direction becomes the "real" consensus; we
                # mutate analysis.confidence to the vote-share %; if
                # consensus DISAGREE or contradicts the plan's
                # direction, the plan is rejected with a
                # `self_consistency:` validator error. Fail-soft on
                # API errors → original confidence preserved, plan
                # passes the gate, log emits SELF_CONSISTENCY_DEGRADED.
                try:
                    import self_consistency as _sc
                    if _sc.is_enabled():
                        # FLO-451: 5-specialist voter (News, Macro, HTF Technical,
                        # Sentiment, Devil's Advocate) replaces the FLO-443/450 uniform
                        # ensemble. Behaviour gated by FLO451_VOTER_MODE
                        # (shadow|confidence|block, default shadow). run_specialist_vote
                        # emits the SPECIALIST_VOTE[_SHADOW] log line itself and is
                        # fail-soft (SKIPPED/degraded on SDK/timeout/3+ABSTAIN).
                        _mode = _sc.voter_mode()
                        _ctx = self._build_specialist_context(candidate)
                        _sv = _sc.run_specialist_vote(candidate, context=_ctx, mode=_mode)
                        if not _sv.degraded:
                            # block mode + 3+ REJECT -> reject the submission.
                            if _mode == "block" and _sv.would_block:
                                _vs = " ".join(
                                    f"{v.name}:{v.vote}:{v.confidence}" for v in _sv.votes
                                )
                                self._log_fail(
                                    "submit_plan_to_snow", start,
                                    f"specialist_block result={_sv.result}",
                                )
                                return {
                                    "success": False, "plan_id": None,
                                    "validation_errors": [
                                        f"specialist_vote: 3+ specialists REJECT this "
                                        f"plan (votes=[{_vs}]). FLO-451 block mode: "
                                        f"reauthor against the objections or WAIT."
                                    ],
                                }
                            # confidence/block mode: apply the capped voter confidence
                            # (min(plan_conf, voter_avg)). shadow mode: log only, the
                            # plan proceeds with its original confidence.
                            if _mode in ("confidence", "block") and isinstance(
                                candidate.get("analysis"), dict
                            ):
                                candidate["analysis"]["confidence"] = _sv.applied_confidence
                except Exception as _sc_err:
                    log.warning(
                        f"SPECIALIST_VOTE_DEGRADED | plan_id="
                        f"{candidate.get('id', '?')} reason="
                        f"{type(_sc_err).__name__}: {_sc_err} | gate inactive (FLO-451)"
                    )

                ok, parsed, errors = _validate(
                    candidate,
                    author_regime=_author_regime,
                    author_calendar=_author_calendar,
                    author_account=_author_account,
                    author_d1_trend=_author_d1_trend,
                    author_setup_ctx=_author_setup_ctx,
                )
                if not ok:
                    self._log_fail(
                        "submit_plan_to_snow",
                        start,
                        f"validation_failed errors={len(errors)}",
                    )
                    return {
                        "success": False, "plan_id": None,
                        "validation_errors": list(errors),
                    }
                _snow_db.insert_plan(parsed)
                # FLO-422 Step 3: passive author-time regime snapshot for
                # lifecycle-sensitive setup_types. Fail-soft — submission
                # never fails because the snapshot path errored. Helper
                # filters by setup_type internally; non-qualifying plans
                # are no-ops.
                _maybe_persist_author_regime_snapshot(parsed)
                # Include cwd + db path in the success log line so future
                # operators auditing `logs/trading_bot_*.log` can tell at
                # a glance whether the call came from production, a pytest
                # run, or an investigation subprocess — without a repeat of
                # the FLO-347 Phase 6.5 misattribution P0.
                try:
                    import os as _os, config as _cfg
                    _db_path = _os.path.abspath(
                        getattr(_cfg, "HISTORY_DB_PATH", "data/history.db")
                    )
                    _cwd = _os.getcwd()
                except Exception:
                    _db_path = "?"
                    _cwd = "?"
                self._log_tool(
                    "submit_plan_to_snow", start,
                    f"ok plan_id={plan_id} attempt={attempt} "
                    f"cwd={_cwd} db={_db_path}",
                )
                # FLO-382 D1: emit Recipe Book adoption diagnostic
                # filtered to a 600s recency window. Buffer is NOT
                # cleared so paired-hedge cycles (two submits in
                # the same second) both see the cycle's pulls. The
                # deque(maxlen=50) bounds memory; recency filter
                # bounds attribution.
                try:
                    from snow.instrumentation import emit_recipe_pulled
                    setup_type: Optional[str] = None
                    try:
                        analysis = getattr(parsed, "analysis", None)
                        if analysis is not None:
                            setup_type = getattr(analysis, "setup_type", None)
                    except Exception:
                        setup_type = None
                    # Recency filter — recipe pulls within last 600s
                    # of this submit count toward this plan. Floki's
                    # cycle is typically <5min; 10min covers paired
                    # hedge cycles + brief defer-and-resume gaps.
                    recent_pulls: list[dict] = []
                    try:
                        import datetime as _dt
                        from tz_utils import utc_now as _utc_now
                        now_dt = _utc_now()
                        for rp in self._recipe_pulls:
                            ts = rp.get("ts")
                            if not ts:
                                continue
                            try:
                                ts_dt = _dt.datetime.fromisoformat(
                                    str(ts).replace("Z", "+00:00")
                                )
                            except Exception:
                                continue
                            if (now_dt - ts_dt).total_seconds() <= 600:
                                recent_pulls.append(rp)
                    except Exception:
                        recent_pulls = list(self._recipe_pulls)
                    emit_recipe_pulled(
                        plan_id=plan_id,
                        recipe_pulls=recent_pulls,
                        final_setup_type=setup_type,
                        plan=parsed,
                    )
                except Exception as _e_diag:
                    try:
                        log.warning(
                            f"snow.plan.recipe_pulled hook failed: {_e_diag}"
                        )
                    except Exception:
                        pass
                return {
                    "success": True, "plan_id": plan_id,
                    "validation_errors": None,
                }
            except _sql.IntegrityError as e:
                # Primary-key collision on plan_id. Retry with a fresh id.
                last_err = f"id_collision: {e}"
                continue
            except Exception as e:
                self._log_fail("submit_plan_to_snow", start, f"error={e}")
                return {"success": False, "reason": f"{type(e).__name__}: {e}"}
        self._log_fail(
            "submit_plan_to_snow", start,
            f"id_collision_retries_exhausted last={last_err}",
        )
        return {"success": False, "reason": "plan_id collision retry exhausted"}

    def cancel_plan(self, plan_id: str, reason: str) -> Dict[str, Any]:
        """Cancel a PENDING Snow plan.

        Only plans in PENDING state can be cancelled via this tool. Plans
        in TRIGGERED / ACTIVE / CLOSING are rejected — an ACTIVE plan
        corresponds to a real broker position; close the position via
        `close_trade(ticket)` instead. Terminal statuses (CLOSED /
        CANCELLED / EXPIRED / FAILED) are also rejected as no-ops.

        `reason` must be a non-empty string (audit requirement).
        """
        start = time.time()
        try:
            from snow import db as _snow_db
        except Exception as e:
            return {"success": False, "reason": f"snow import failed: {e}"}

        if not isinstance(plan_id, str) or not plan_id.strip():
            return {"success": False, "reason": "plan_id must be a non-empty string"}
        if not isinstance(reason, str) or not reason.strip():
            return {"success": False, "reason": "reason must be a non-empty string (audit)"}

        try:
            row = _snow_db.get_plan(plan_id)
            if row is None:
                self._log_fail("cancel_plan", start, f"plan_not_found={plan_id}")
                return {"success": False, "reason": f"plan {plan_id} not found"}
            current_status = row.get("status")
            if current_status != "pending":
                if current_status in ("triggered", "active", "closing"):
                    msg = (
                        f"plan {plan_id} is {current_status}; cannot cancel an "
                        f"active plan — close the broker position via "
                        f"close_trade(ticket) if needed"
                    )
                else:
                    msg = (
                        f"plan {plan_id} is {current_status} (terminal); "
                        f"nothing to cancel"
                    )
                self._log_fail("cancel_plan", start, f"bad_state={current_status}")
                return {
                    "success": False, "reason": msg,
                    "current_status": current_status,
                }

            # FLO-374: terminal transition stamps closed_at.
            _snow_db.mark_plan_terminal(plan_id, "cancelled")
            # Audit row — tracks WHO cancelled WHEN with WHICH reason.
            try:
                _snow_db.record_trigger(
                    plan_id=plan_id,
                    contingency_name="_user_cancel",
                    contingency_kind="entry",
                    action_type="cancel_plan",
                    execution_status="success",
                    action_params={"reason": reason.strip()},
                )
            except Exception as audit_err:
                # Audit failure should NOT hide a successful state change
                # from Floki; log + continue. Plan IS cancelled.
                log.error(f"cancel_plan audit row failed: {audit_err}")

            self._log_tool("cancel_plan", start, f"ok plan_id={plan_id}")
            return {
                "success": True, "plan_id": plan_id,
                "new_status": "cancelled", "reason": reason.strip(),
            }
        except Exception as e:
            self._log_fail("cancel_plan", start, f"error={e}")
            return {"success": False, "reason": f"{type(e).__name__}: {e}"}

    def override_opposing_block(self, plan_id: str, reason: str) -> Dict[str, Any]:
        """FLO-418 — bypass the opposing-positions gate for a single
        Snow plan. Allows both BUY and SELL to be open simultaneously
        on the same symbol if Floki has explicit reason to want both
        legs (e.g. complementary setups, hedge thesis).

        Use this ONLY in response to a <snow_pending_decisions> block.
        Default behaviour for opposing positions is the FLO-85 gate
        (refuse to open opposing) — this tool is the explicit override
        per CEO directive.

        After override stamp: Snow's next 5s tick on the plan bypasses
        the opposing detection and fires the entry normally. The
        override has a 5-minute TTL — preventing a stale override
        from re-triggering on a future opposing scenario.
        """
        start = time.time()
        try:
            from snow import db as _snow_db

            if not isinstance(plan_id, str) or not plan_id.strip():
                return {"success": False, "reason": "plan_id must be a non-empty string"}
            if not isinstance(reason, str) or not reason.strip():
                return {"success": False, "reason": "reason required (audit)"}
            if len(reason) > 500:
                return {"success": False, "reason": "reason must be <= 500 chars"}

            row = _snow_db.get_plan(plan_id)
            if row is None:
                self._log_fail("override_opposing_block", start, f"plan_not_found={plan_id}")
                return {"success": False, "reason": f"plan {plan_id} not found"}
            current_status = row.get("status")
            if current_status != "pending":
                self._log_fail(
                    "override_opposing_block", start,
                    f"bad_state={current_status}",
                )
                return {
                    "success": False,
                    "reason": (
                        f"plan {plan_id} is {current_status}; "
                        f"override only applies to pending plans"
                    ),
                }

            _snow_db.set_override_opposing(plan_id, ttl_seconds=300)
            try:
                _snow_db.record_trigger(
                    plan_id=plan_id,
                    contingency_name="_floki_override_opposing",
                    contingency_kind="entry",
                    action_type="override_opposing_block",
                    execution_status="success",
                    action_params={"reason": reason.strip(), "ttl_seconds": 300},
                )
            except Exception as audit_err:
                log.error(f"override_opposing_block audit row failed: {audit_err}")

            self._log_tool(
                "override_opposing_block", start,
                f"ok plan_id={plan_id} ttl=300s",
            )
            return {
                "success": True,
                "plan_id": plan_id,
                "ttl_seconds": 300,
                "reason": reason.strip(),
                "note": "Snow will bypass opposing-positions gate for this plan on the next tick.",
            }
        except Exception as e:
            self._log_fail("override_opposing_block", start, f"error={e}")
            return {"success": False, "reason": f"{type(e).__name__}: {e}"}

    def get_plan_status(self, plan_id: str) -> Dict[str, Any]:
        """Return a summary of a Snow plan's current state.

        Returns the mutable DB-column fields only (status, ticket,
        timestamps, outcome) — NOT the full plan_json, which is frozen
        at submit time and consumes significant context. For the full
        plan schema, Floki already submitted it; he knows what he wrote.
        """
        start = time.time()
        try:
            from snow import db as _snow_db
        except Exception as e:
            return {"success": False, "reason": f"snow import failed: {e}"}

        if not isinstance(plan_id, str) or not plan_id.strip():
            return {"success": False, "reason": "plan_id must be a non-empty string"}

        try:
            row = _snow_db.get_plan(plan_id)
            if row is None:
                self._log_no_cache("get_plan_status", start, f"not_found={plan_id}")
                return {"success": False, "reason": f"plan {plan_id} not found"}
            summary = {
                "plan_id": row.get("id"),
                "status": row.get("status"),
                "created_at": row.get("created_at"),
                "expires_at": row.get("expires_at"),
                "trade_ticket": row.get("trade_ticket"),
                "entered_at": row.get("entered_at"),
                "closed_at": row.get("closed_at"),
                "outcome_pips": row.get("outcome_pips"),
                "outcome_usd": row.get("outcome_usd"),
                "last_evaluated_at": row.get("last_evaluated_at"),
            }
            self._log_tool("get_plan_status", start, f"ok plan_id={plan_id}")
            return {"success": True, **summary}
        except Exception as e:
            self._log_fail("get_plan_status", start, f"error={e}")
            return {"success": False, "reason": f"{type(e).__name__}: {e}"}

    @staticmethod
    def _scan_missing_required_fields(
        plan_dict: Optional[Dict[str, Any]],
    ) -> list:
        """FLO-408 Phase 2 — Layer C defense. Returns dotted-path
        strings for every required field missing from the plan dict.

        Required field sets match the Pydantic Plan schema:
          analysis: thesis, key_levels, confidence, regime_assumed,
                    setup_type, context_tags, confidence_reason
          entry: direction, volume, conditions, initial_sl, initial_tp,
                 entry_price
          management[i]: name, priority, conditions, action, fires
          exit[i]: name, priority, conditions, action, fires
          emergency: max_loss_pips, max_duration_minutes,
                     on_broker_error

        SKIPPED (auto-stamped by handler or schema-defaulted):
          id, created_at, created_by, status, schema_version, expires_at

        Pure function. Same wrapper-or-direct unwrap as
        _scan_null_object_paths so partial+wrapped Gemini payloads
        evaluate against the canonical inner-plan view.
        """
        if not isinstance(plan_dict, dict):
            return []
        outer = plan_dict
        inner = plan_dict.get("plan")
        if isinstance(inner, dict) and "analysis" in inner:
            outer = inner

        missing: list = []

        # FLO-366: setup_type / context_tags / confidence_reason are
        # required ONLY for schema_version >= 3. v1/v2 plans round-
        # trip without them. Mirror the Pydantic Plan._check_v3_
        # tagging_required validator's conditional. Default to
        # current SCHEMA_VERSION when absent (Pydantic default).
        try:
            from snow.schema import SCHEMA_VERSION as _SCHEMA_DEFAULT
        except Exception:
            _SCHEMA_DEFAULT = 3
        _v = outer.get("schema_version", _SCHEMA_DEFAULT)
        try:
            _v = int(_v)
        except Exception:
            _v = _SCHEMA_DEFAULT
        _is_v3_plus = _v >= 3

        # analysis.* — block + Pydantic-required fields.
        analysis = outer.get("analysis")
        if "analysis" not in outer:
            missing.append("analysis (entire block)")
        elif isinstance(analysis, dict):
            # Pydantic-required at any version: thesis, confidence.
            # NOT scanned: key_levels (default factory), regime_assumed
            # (Optional).
            for f in ("thesis", "confidence"):
                if f not in analysis:
                    missing.append(f"analysis.{f}")
            # FLO-366 v3+ required: setup_type, context_tags,
            # confidence_reason.
            if _is_v3_plus:
                for f in ("setup_type", "context_tags",
                          "confidence_reason"):
                    if f not in analysis:
                        missing.append(f"analysis.{f}")

        # entry.* — block + per-field
        entry = outer.get("entry")
        if "entry" not in outer:
            missing.append("entry (entire block)")
        elif isinstance(entry, dict):
            # entry_price is Optional (FLO-392 hint) — NOT scanned.
            for f in ("direction", "volume", "conditions",
                      "initial_sl", "initial_tp"):
                if f not in entry:
                    missing.append(f"entry.{f}")

        # management[i].* — block check is permissive (empty list ok),
        # per-item required fields enforced
        management = outer.get("management")
        if isinstance(management, list):
            for i, item in enumerate(management):
                if not isinstance(item, dict):
                    continue  # null/string handled by other scanners
                # priority (default 5), fires (default once) — NOT scanned.
                for f in ("name", "conditions", "action"):
                    if f not in item:
                        missing.append(f"management[{i}].{f}")

        # exit[i].* — FLO-401 ≥1 enforced by Pydantic; here we just
        # check per-item required fields. Block-missing flagged.
        exits = outer.get("exit")
        if "exit" not in outer:
            missing.append("exit (entire block)")
        elif isinstance(exits, list):
            for i, item in enumerate(exits):
                if not isinstance(item, dict):
                    continue
                # priority (default 5), fires (default once) — NOT scanned.
                for f in ("name", "conditions", "action"):
                    if f not in item:
                        missing.append(f"exit[{i}].{f}")

        # emergency block — has default_factory=EmergencyBlock with
        # all sub-fields defaulted. Plan validates without it. NOT
        # scanned (Pydantic accepts an absent emergency block AND
        # accepts a partial one). Auto-fills via Pydantic defaults.

        return missing

    @staticmethod
    def _scan_null_object_paths(plan_dict: Optional[Dict[str, Any]]) -> list:
        """FLO-404 v3 — Layer B defense. Returns dotted-path strings for
        every position in the plan dict where Pydantic expects a
        populated object but Gemini emitted `null`.

        Paths checked (matches the Pydantic schema's non-Optional
        nested fields):
          analysis.context_tags        — must be dict
          entry.conditions[i]          — each must be dict (i = 0..N-1)
          management[i]                — each must be dict
          exit[i]                      — each must be dict

        Designed to be safe on partial / malformed input — if the outer
        plan structure is itself malformed (non-dict, missing entry
        block, etc.), we return only the paths we CAN reach. Pydantic
        catches the rest. Pure function, no side effects, no MT5 / DB.
        """
        if not isinstance(plan_dict, dict):
            return []
        # Plans can arrive wrapped ({"plan": {...}}) or direct. Unwrap
        # once if the outer dict has a `plan` key holding a dict that
        # itself looks like a plan body.
        outer = plan_dict
        inner = plan_dict.get("plan")
        if isinstance(inner, dict) and "analysis" in inner:
            outer = inner

        bad_paths: list = []
        # analysis.context_tags
        analysis = outer.get("analysis")
        if isinstance(analysis, dict) and "context_tags" in analysis:
            if analysis["context_tags"] is None:
                bad_paths.append("analysis.context_tags")

        # entry.conditions[*]
        entry = outer.get("entry")
        if isinstance(entry, dict):
            conds = entry.get("conditions")
            if isinstance(conds, list):
                for i, c in enumerate(conds):
                    if c is None:
                        bad_paths.append(f"entry.conditions[{i}]")

        # management[*] and exit[*]
        for key in ("management", "exit"):
            items = outer.get(key)
            if isinstance(items, list):
                for i, item in enumerate(items):
                    if item is None:
                        bad_paths.append(f"{key}[{i}]")

        return bad_paths

    @staticmethod
    def _plan_target_zone_touched(
        plan_dict: Optional[Dict[str, Any]], created_at_iso: Optional[str],
    ) -> Optional[bool]:
        """FLO-404 staleness signal — True if price has reached the
        plan's directional target since the plan was created.

        Definition:
          BUY plan  → max(key_levels)  reached if MT5-high  ≥ that level since create
          SELL plan → min(key_levels)  reached if MT5-low   ≤ that level since create

        Returns None when the signal cannot be computed (no MT5, no
        key_levels, no direction, plan too old/fresh, broker query
        failure). None means "no opinion" — Floki should treat absence
        of the signal as no information rather than a False.

        Cost: one mt5.copy_rates_from_pos M1 query per plan in the
        list. With the typical 1-4 active plans this is bounded.
        """
        if not isinstance(plan_dict, dict) or not created_at_iso:
            return None
        direction = ((plan_dict.get("entry") or {}).get("direction") or "").upper()
        key_levels = (plan_dict.get("analysis") or {}).get("key_levels") or []
        if direction not in ("BUY", "SELL") or not key_levels:
            return None
        try:
            from datetime import datetime, timezone
            from mt5_safe import mt5
            import MetaTrader5 as _mt5_raw
        except Exception:
            return None
        try:
            created_dt = datetime.fromisoformat(
                str(created_at_iso).replace("Z", "+00:00")
            )
        except Exception:
            return None
        try:
            minutes_since = int(
                (datetime.now(timezone.utc) - created_dt).total_seconds() / 60
            )
        except Exception:
            return None
        if minutes_since < 0 or minutes_since > 1440:
            # < 0 = future-dated; > 24h = too old to compute cheaply
            return None
        bars_count = min(max(minutes_since + 60, 60), 1500)
        try:
            rates = mt5.copy_rates_from_pos(
                "XAUUSD", _mt5_raw.TIMEFRAME_M1, 0, bars_count,
            )
        except Exception:
            return None
        if rates is None or len(rates) == 0:
            return None
        # MT5 candle.time is broker-local epoch — subtract offset to
        # get true UTC for filtering against plan.created_at.
        try:
            from executor import _mt5_server_offset
            offset_s = int(_mt5_server_offset() or 0)
        except Exception:
            offset_s = 10800  # 3h default per FLO-96
        try:
            created_epoch = int(created_dt.timestamp())
            relevant = [
                r for r in rates
                if (int(r["time"]) - offset_s) >= created_epoch
            ]
            if not relevant:
                return False  # too fresh; no candle has fully formed since create
            if direction == "BUY":
                target = float(max(key_levels))
                max_high = max(float(r["high"]) for r in relevant)
                return max_high >= target
            else:  # SELL
                target = float(min(key_levels))
                min_low = min(float(r["low"]) for r in relevant)
                return min_low <= target
        except Exception:
            return None

    def _build_specialist_context(self, candidate: Dict[str, Any]) -> Dict[str, Any]:
        """FLO-451 — assemble CHEAP, cached context for the specialist voters.

        Reads `data/bot_state.json` only (no live tool re-runs, no Luna/Echo LLM
        calls during submission): the Technical voter gets multi_tf_indicators +
        market_regime; the Macro voter gets market_context as a starting hint
        (its web search fills the rest). Price comes from last_known_price.
        All fail-soft — a missing/corrupt state file yields a minimal context and
        the web-search voters still function.
        """
        from datetime import datetime as _dt, timezone as _tz
        ctx: Dict[str, Any] = {"as_of_iso": _dt.now(_tz.utc).strftime("%Y-%m-%d %H:%M")}
        try:
            bs_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "data", "bot_state.json"
            )
            with open(bs_path, "r", encoding="utf-8") as _f:
                _d = json.load(_f)
            ctx["price"] = _d.get("last_known_price")
            ctx["multi_tf"] = {
                "multi_tf_indicators": _d.get("multi_tf_indicators"),
                "market_regime": _d.get("market_regime"),
            }
            ctx["dxy"] = _d.get("market_context")
        except Exception:
            pass
        return ctx

    def list_active_plans(self, ticket: Optional[int] = None) -> Dict[str, Any]:
        """List all Snow plans in non-terminal states.

        Returns summaries — id, status, trade_ticket, created_at,
        last_evaluated_at, plus the four FLO-404 duplicate-avoidance
        fields (direction, entry_price, thesis, expires_at) Floki
        asked for in his data_needs feedback. Full plan_json stays
        out of the summary (callers that need it can use
        get_plan_status). Optional `ticket` filter narrows to plans
        attached to a specific broker ticket.
        """
        start = time.time()
        try:
            from snow import db as _snow_db
        except Exception as e:
            return {"success": False, "reason": f"snow import failed: {e}"}

        try:
            # FLO-449: route the tool read explicitly through snow.db's
            # read-only (autocommit) connection helper — never a writer/
            # cached connection. Behaviourally identical to
            # get_active_plans() (which already uses _connect_read_only via
            # list_plans_by_status); the dedicated accessor makes the
            # read-only contract visible at the tool boundary. Does NOT fix
            # the agent_sdk-subprocess staleness — see snow.db docstring.
            rows = _snow_db.get_active_plans_read_only()
            if ticket is not None:
                try:
                    t = int(ticket)
                    rows = [r for r in rows if r.get("trade_ticket") == t]
                except (TypeError, ValueError):
                    return {
                        "success": False,
                        "reason": f"ticket must be an int, got {type(ticket).__name__}",
                    }
            plans = []
            for r in rows:
                # Parse plan_json defensively — direction/entry_price/
                # thesis live inside the JSON blob, not as columns.
                # Any parse failure falls through to None per field
                # so a corrupted row still surfaces id/status without
                # crashing the whole list.
                _direction: Optional[str] = None
                _entry_price = None
                _thesis: Optional[str] = None
                _pj: Optional[Dict[str, Any]] = None
                _raw = r.get("plan_json")
                if _raw:
                    try:
                        _pj = json.loads(_raw) if isinstance(_raw, str) else _raw
                        _entry = (_pj or {}).get("entry") or {}
                        _analysis = (_pj or {}).get("analysis") or {}
                        _direction = _entry.get("direction")
                        _entry_price = _entry.get("entry_price")
                        _thesis = _analysis.get("thesis")
                    except Exception:
                        _pj = None
                # FLO-404 staleness signal (CEO directive 2026-04-30):
                # has price reached the plan's directional target since
                # creation? True/False/None. Floki uses this with the
                # EVALUATE EXISTING PLANS rule to decide whether the
                # thesis already played out — cancel + re-author rather
                # than holding a stale plan past its target.
                _target_touched = AgentTools._plan_target_zone_touched(
                    _pj, r.get("created_at"),
                )
                plans.append({
                    "plan_id": r.get("id"),
                    "status": r.get("status"),
                    "trade_ticket": r.get("trade_ticket"),
                    "created_at": r.get("created_at"),
                    "last_evaluated_at": r.get("last_evaluated_at"),
                    # FLO-404 duplicate-avoidance fields (CEO directive
                    # 2026-04-29 — Floki self-requested in data_needs).
                    "direction": _direction,
                    "entry_price": _entry_price,
                    "thesis": _thesis,
                    "expires_at": r.get("expires_at"),
                    # FLO-404 staleness signal (CEO directive 2026-04-30).
                    "target_zone_touched": _target_touched,
                })
            self._log_tool(
                "list_active_plans", start,
                f"ok count={len(plans)}" + (f" ticket={ticket}" if ticket else ""),
            )
            return {"success": True, "count": len(plans), "plans": plans}
        except Exception as e:
            self._log_fail("list_active_plans", start, f"error={e}")
            return {"success": False, "reason": f"{type(e).__name__}: {e}"}

    def _agent_monitor_events_path(self) -> str:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        data_dir = os.path.join(base_dir, "data")
        os.makedirs(data_dir, exist_ok=True)
        return os.path.join(data_dir, "agent_monitor_events.json")

    def _load_watch_conditions(self) -> Dict[str, Any]:
        path = self._watch_conditions_path()
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
