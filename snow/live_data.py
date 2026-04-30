"""Snow live-data layer — fresh MT5 ticks + M1 bars + per-tick indicators.

This module is Snow's FIRST MT5 touch and the boundary enforced by Rule 23:
  * Every MT5 call goes through `from mt5_safe import mt5, mt5_lock`.
  * `import MetaTrader5 as mt5` is FORBIDDEN anywhere in this file.
  * Read-only calls only in Phase 3a — no order_send, no modify.

Design per RFC §6.2:
  * `refresh()` runs ONCE per Snow tick (every ~5 s). Pulls the current
    tick + a rolling window of M1 bars. Clears the indicator cache.
  * Indicator accessors (`rsi`, `macd_histogram`, `ema`, `atr`) are
    tick-cached: the first call computes and memoises; subsequent calls
    in the same tick return the memoised value. Zero recomputation cost
    when many contingencies ask for the same (tf, indicator, period)
    tuple within one tick.
  * M1-timeframe indicators compute locally from the fresh bar window.
  * H1+ timeframe indicators delegate to `SemanticCache` (Floki's
    cycle-level data) per RFC §6.1 — staleness acceptable for these.

Fidelity note (RFC §14.3 item 3):
  Formulas are ported byte-for-byte from `technical_analyzer.py`:
    * EMA(N) = `close.ewm(span=N, adjust=False).mean()`
    * RSI(14) = SMA-seeded + Wilder exponential smoothing loop
    * MACD = EMA(12) − EMA(26), signal = EMA(9) of MACD, hist = MACD − signal
    * ATR(14) = True Range rolling 14-period SMA (price units)
  RFC §6.3 suggested "pure-Python (numpy optional)"; we use pandas
  instead because the higher-order requirement in §14.3 ("port formulas
  byte-for-byte") wins over the dependency-minimisation preference.
  Snow already runs in-process with the bot, which has pandas loaded.

Graceful degradation (CEO Phase 3a directive):
  Every public method returns `None` rather than raising when any of:
    (a) MT5 disconnected — `symbol_info_tick` returns None,
    (b) invalid symbol — same,
    (c) empty bar array,
    (d) too few bars for the indicator's window,
    (e) MT5 call raises (timeout, terminal crash).
  The Snow loop treats None as "data missing" and evaluates the
  owning condition as False (RFC §6.5). No exception ever propagates
  out of `refresh()` or the accessors.
"""
from __future__ import annotations

from typing import Any, Optional, Set, Tuple

import pandas as pd

from mt5_safe import mt5, mt5_lock
from snow.semantic_cache import SemanticCache


# M1 bar window. 120 bars = 2 h @ M1.
# Sized for the widest indicator we compute (MACD needs 26 + 9 = 35
# warm-up bars; ATR needs 14; RSI needs ~30 for Wilder stability). 120
# gives margin without blowing memory.
M1_BAR_COUNT: int = 120

# Indicator-period floor: below this number of available bars, the
# accessor returns None. Matches technical_analyzer.py's guard `len(df) < 50`
# at a lower bar — Snow works with 120-bar windows that are denser than
# Brain's H1 windows, so 30 bars is the minimum that produces a stable
# Wilder-smoothed RSI.
MIN_BARS_FOR_INDICATOR: int = 30


class LiveData:

    # FLO-410: process-lifetime dedup for unsupported-TF warnings on
    # bollinger / stochastic / macd_divergence (Brain publishes these
    # H1-only). Class-level so all LiveData instances share one record;
    # a bot restart resets it (desirable — operators see post-deploy
    # plans re-warn if still asking for unsupported data).
    _warned_unsupported_tf: Set[Tuple[str, str]] = set()

    def __init__(self, symbol: str, semantic_cache: SemanticCache):
        self._symbol = symbol
        self._semantic = semantic_cache
        self._last_tick = None            # raw MT5 tick namedtuple or None
        self._m1_bars: Optional[pd.DataFrame] = None
        # Tick-scoped indicator cache — cleared at the START of every
        # refresh() call.  Keyed by (tf, indicator_name, period).
        self._indicator_cache: dict[tuple, Optional[float]] = {}

    # -- Lifecycle -----------------------------------------------------------

    def refresh(self) -> None:
        """Pull a fresh tick + M1 bar window for this tick. Must be
        called once per Snow tick before any accessor runs.

        The indicator cache is cleared BEFORE the MT5 calls so that if
        a fetch raises mid-way, stale cached values from the previous
        tick cannot leak into this tick's accessor calls. Corollary:
        after an exception, accessors see no tick data AND no cached
        indicators — all return None — which is the fail-safe state
        Snow's loop expects (RFC §6.5).
        """
        self._indicator_cache.clear()
        try:
            with mt5_lock:
                tick = mt5.symbol_info_tick(self._symbol)
                rates = mt5.copy_rates_from_pos(
                    self._symbol, mt5.TIMEFRAME_M1, 0, M1_BAR_COUNT
                )
        except Exception:
            self._last_tick = None
            self._m1_bars = None
            return

        self._last_tick = tick  # may be None on disconnect
        if rates is None or len(rates) == 0:
            self._m1_bars = None
        else:
            self._m1_bars = pd.DataFrame(rates)

    # -- Price ---------------------------------------------------------------

    def price(self, side: str = "mid") -> Optional[float]:
        """Return current bid / ask / mid, or None if disconnected.

        `side` is one of: "bid", "ask", "mid". Any other value returns
        None — condition evaluator will see missing data and return
        False.
        """
        if self._last_tick is None:
            return None
        bid = getattr(self._last_tick, "bid", None)
        ask = getattr(self._last_tick, "ask", None)
        if bid is None or ask is None:
            return None
        if side == "bid":
            return float(bid)
        if side == "ask":
            return float(ask)
        if side == "mid":
            return float((bid + ask) / 2.0)
        return None

    # -- Indicators (M1 = live; H1+ = semantic) ------------------------------

    def rsi(self, tf: str = "M1", period: int = 14) -> Optional[float]:
        """Latest RSI value for the given timeframe.

        M1 → computed locally from the fresh bar window.
        Anything else → read from dp.multi_tf_indicators[tf].rsi
        (FLO-410: was previously routing through TF-agnostic
        _semantic_indicator and silently returning H1 data for every
        non-M1 request).
        """
        if tf != "M1":
            return self._multi_tf_indicator(tf, "rsi")
        return self._tick_cached(("M1", "rsi", period), self._compute_rsi, period)

    def macd_histogram(self, tf: str = "M1") -> Optional[float]:
        """Latest MACD histogram value.
        M1 → local. Non-M1 → mtf[tf].macd.histogram (FLO-410)."""
        if tf != "M1":
            return self._multi_tf_indicator(tf, "macd", "histogram")
        return self._tick_cached(("M1", "macd_hist", 0), self._compute_macd_hist)

    def ema(self, tf: str = "M1", period: int = 9) -> Optional[float]:
        """Latest EMA value.
        M1 → local. Non-M1 → mtf[tf].ema<period> (FLO-410)."""
        if tf != "M1":
            return self._multi_tf_indicator(tf, f"ema{period}")
        return self._tick_cached(("M1", "ema", period), self._compute_ema, period)

    def atr(self, tf: str = "M1", period: int = 14) -> Optional[float]:
        """Latest ATR value (price units).
        M1 → local. Non-M1 → mtf[tf].atr (FLO-410)."""
        if tf != "M1":
            return self._multi_tf_indicator(tf, "atr")
        return self._tick_cached(("M1", "atr", period), self._compute_atr, period)

    # -- Phase 7.3 (FLO-355) Cat A indicator accessors ---------------------
    # All four read from Brain's SemanticCache snapshot; no LiveData
    # computation. Brain currently publishes these on its primary
    # timeframe (H1) only — non-H1 calls return None (fail-safe at
    # evaluator level: missing data → False).

    def bollinger(self, tf: str = "H1") -> Optional[dict]:
        """Return the Bollinger dict for `tf` or None.
        Shape: {upper, middle, lower, position (0..1), squeeze (bool)}.
        The `position` is normalised: 0 == lower band, 1 == upper band,
        >1 == above upper, <0 == below lower.

        FLO-411: was H1-only; now reads from
        dp.multi_tf_indicators[tf].bollinger for non-H1 (Brain's
        compute_indicators_from_candles writes the same shape per TF).
        H1 path keeps reading dp.indicators.bollinger for backward compat
        with the slow-cycle producer."""
        if tf != "H1":
            mtf = self._semantic.get("multi_tf_indicators")
            if not isinstance(mtf, dict):
                return None
            tf_block = mtf.get(tf)
            if not isinstance(tf_block, dict):
                return None
            bb = tf_block.get("bollinger")
            return bb if isinstance(bb, dict) else None
        ind = self._semantic.get("indicators")
        if not isinstance(ind, dict):
            return None
        bb = ind.get("bollinger")
        return bb if isinstance(bb, dict) else None

    def stochastic(self, tf: str = "H1") -> Optional[float]:
        """Return the stochastic %K value (0-100) for `tf` or None.

        FLO-411: was H1-only; now reads dp.multi_tf_indicators[tf].
        stochastic.value for non-H1."""
        if tf != "H1":
            return self._multi_tf_indicator(tf, "stochastic", "value")
        ind = self._semantic.get("indicators")
        if not isinstance(ind, dict):
            return None
        st = ind.get("stochastic")
        if isinstance(st, dict):
            v = st.get("value")
            if isinstance(v, (int, float)):
                return float(v)
        return None

    def pivot_points(self) -> Optional[dict]:
        """Return Brain's daily pivot dict or None.
        Shape: {classic: {PP,R1,R2,R3,S1,S2,S3}, fibonacci: {...}, source}.
        Brain's `pivot_points` may be wrapped in `{daily: ...}` (multi-
        layer) or be the dict itself; we accept both shapes."""
        pp = self._semantic.get("pivot_points")
        if not isinstance(pp, dict):
            return None
        # Multi-layer wrapper: {"daily": {...}, "weekly": {...}}
        if isinstance(pp.get("daily"), dict):
            return pp["daily"]
        # Direct shape: {"classic": {...}, "fibonacci": {...}}
        if isinstance(pp.get("classic"), dict) or isinstance(pp.get("fibonacci"), dict):
            return pp
        return None

    def macd_divergence(self, tf: str = "H1") -> Optional[dict]:
        """Return the MACD divergence detection for `tf` or None.
        Shape: {detected: bool, type: 'bullish'|'bearish'|None, bars_since}.

        FLO-411: was H1-only; now reads dp.multi_tf_indicators[tf].
        macd.divergence for non-H1. compute_indicators_from_candles
        runs detect_macd_divergence per TF and writes the result into
        the macd sub-dict."""
        if tf != "H1":
            mtf = self._semantic.get("multi_tf_indicators")
            if not isinstance(mtf, dict):
                return None
            tf_block = mtf.get(tf)
            if not isinstance(tf_block, dict):
                return None
            macd_block = tf_block.get("macd")
            if isinstance(macd_block, dict):
                div = macd_block.get("divergence")
                if isinstance(div, dict):
                    return div
            return None
        ind = self._semantic.get("indicators")
        if not isinstance(ind, dict):
            return None
        macd = ind.get("macd")
        if isinstance(macd, dict):
            div = macd.get("divergence")
            if isinstance(div, dict):
                return div
        return None

    # -- Internals -----------------------------------------------------------

    def _tick_cached(
        self,
        key: tuple,
        compute_fn,
        *args,
    ) -> Optional[float]:
        """Memoise `compute_fn(*args)` for the current tick."""
        if key in self._indicator_cache:
            return self._indicator_cache[key]
        val = compute_fn(*args)
        self._indicator_cache[key] = val
        return val

    def _bars_ready(self, min_bars: int) -> bool:
        """True iff we have enough M1 bars to produce a stable indicator."""
        return (
            self._m1_bars is not None
            and len(self._m1_bars) >= max(min_bars, MIN_BARS_FOR_INDICATOR)
        )

    # --- Indicator computations (formulas mirror technical_analyzer.py) ----

    def _compute_ema(self, period: int) -> Optional[float]:
        if not self._bars_ready(period):
            return None
        series = self._m1_bars["close"].ewm(span=period, adjust=False).mean()
        val = series.iloc[-1]
        return float(val) if pd.notna(val) else None

    def _compute_rsi(self, period: int) -> Optional[float]:
        if not self._bars_ready(period + 1):
            return None
        close = self._m1_bars["close"]
        delta = close.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = (-delta.where(delta < 0, 0.0))
        # SMA seed (first `period` values) then Wilder's smoothing.
        # Exactly mirrors technical_analyzer.py:65-73.
        avg_gain = gain.rolling(window=period).mean().copy()
        avg_loss = loss.rolling(window=period).mean().copy()
        n = len(close)
        for i in range(period + 1, n):
            avg_gain.iloc[i] = (avg_gain.iloc[i - 1] * (period - 1) + gain.iloc[i]) / period
            avg_loss.iloc[i] = (avg_loss.iloc[i - 1] * (period - 1) + loss.iloc[i]) / period
        # Replicate `rs = avg_gain / avg_loss.replace(0, np.nan)` without
        # importing numpy: pandas' division yields inf or nan when loss==0,
        # and the subsequent fillna(50.0) handles those cases.
        rs = avg_gain / avg_loss.where(avg_loss != 0)
        rsi = 100 - (100 / (1 + rs))
        rsi = rsi.fillna(50.0)
        val = rsi.iloc[-1]
        return float(val) if pd.notna(val) else None

    def _compute_macd_hist(self) -> Optional[float]:
        # MACD needs at least 26 + 9 = 35 bars for a stable histogram.
        if not self._bars_ready(35):
            return None
        close = self._m1_bars["close"]
        ema_12 = close.ewm(span=12, adjust=False).mean()
        ema_26 = close.ewm(span=26, adjust=False).mean()
        macd = ema_12 - ema_26
        signal = macd.ewm(span=9, adjust=False).mean()
        hist = macd - signal
        val = hist.iloc[-1]
        return float(val) if pd.notna(val) else None

    def _compute_atr(self, period: int) -> Optional[float]:
        if not self._bars_ready(period + 1):
            return None
        df = self._m1_bars
        high_low = df["high"] - df["low"]
        high_close = (df["high"] - df["close"].shift()).abs()
        low_close = (df["low"] - df["close"].shift()).abs()
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        # Rolling SMA, not Wilder — matches technical_analyzer.py:95
        atr_series = tr.rolling(window=period).mean()
        val = atr_series.iloc[-1]
        return float(val) if pd.notna(val) else None

    # --- Semantic delegation (H1+ indicators) ------------------------------

    def _semantic_indicator(self, name: str) -> Optional[float]:
        """Pull a single indicator value out of Floki's cached snapshot.

        `_last_agent_data["indicators"]` has sub-dicts like
        `{"rsi": {"value": 62.4, ...}, "macd": {"histogram": 0.3, ...}}`
        — see agent_data_builder._format_indicators. We look up by
        flat name first, falling back to common nested shapes so the
        cache adapter stays tolerant of minor structure drift.
        """
        indicators = self._semantic.get("indicators")
        if not isinstance(indicators, dict):
            return None
        # Flat lookup: `indicators[name]` as a scalar
        val = indicators.get(name)
        if isinstance(val, (int, float)):
            return float(val)
        # Nested lookup: `indicators[name]["value"]` or `["histogram"]`
        if isinstance(val, dict):
            for key in ("value", "histogram"):
                inner = val.get(key)
                if isinstance(inner, (int, float)):
                    return float(inner)
        return None

    # --- FLO-410: Multi-TF semantic delegation -----------------------------

    def _multi_tf_indicator(self, tf: str, *path: str) -> Optional[float]:
        """Read indicators from `dp.multi_tf_indicators[tf]` honoring
        the requested timeframe.

        Brain populates `dp["multi_tf_indicators"][tf]` per-TF for every
        TF in {M1, M5, M15, H1, H4, D1} via
        technical_analyzer.compute_indicators_from_candles. The shape
        is uniform across timeframes:
            {rsi, atr, ema9, ema21, ema50, ema200,
             macd: {value, signal, histogram}, ...}

        `path` is the field-or-nested-field sequence to walk after
        landing on the per-TF block. e.g. ("rsi",) for rsi,
        ("macd", "histogram") for the histogram, ("ema9",) for EMA9.

        Returns None on any missing segment or non-numeric leaf
        (fail-safe per RFC §6.5: missing data → False at the
        evaluator level)."""
        mtf = self._semantic.get("multi_tf_indicators")
        if not isinstance(mtf, dict):
            return None
        block = mtf.get(tf)
        if not isinstance(block, dict):
            return None
        node: Any = block
        for seg in path:
            if not isinstance(node, dict) or seg not in node:
                return None
            node = node[seg]
        return float(node) if isinstance(node, (int, float)) else None

    # --- FLO-410: Warn-once for unsupported-TF requests --------------------

    def _warn_unsupported_tf(self, accessor: str, tf: str) -> None:
        """Log a one-time WARN per (accessor, tf) when a plan requests
        an indicator on a timeframe Brain doesn't compute per-TF.

        Currently bollinger / stochastic / macd_divergence are H1-only
        in Brain's compute pipeline (see technical_analyzer.py). Floki
        may author a plan with `bollinger_position(tf="M5")`; the
        evaluator will silently see None → False every tick. Warning
        once per (accessor, tf) makes the silent-False pattern
        visible to operators without log-spam.

        Dedup is process-lifetime (a class-level set). A bot restart
        re-emits the warning, which is desirable — operators see the
        unsupported request reappear post-deploy."""
        key = (accessor, tf)
        if key in LiveData._warned_unsupported_tf:
            return
        LiveData._warned_unsupported_tf.add(key)
        try:
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "snow.live_data.unsupported_tf accessor=%s tf=%s — "
                "Brain doesn't compute %s per-TF; evaluator will "
                "return False until plan switches to H1 or Brain is "
                "extended.",
                accessor, tf, accessor,
            )
        except Exception:
            pass
