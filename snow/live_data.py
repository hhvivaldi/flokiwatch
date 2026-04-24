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

from typing import Optional

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
        Anything else → delegated to SemanticCache (Floki's cycle data).
        """
        if tf != "M1":
            return self._semantic_indicator("rsi")
        return self._tick_cached(("M1", "rsi", period), self._compute_rsi, period)

    def macd_histogram(self, tf: str = "M1") -> Optional[float]:
        """Latest MACD histogram value. M1 → local; H1+ → semantic."""
        if tf != "M1":
            return self._semantic_indicator("macd_hist")
        return self._tick_cached(("M1", "macd_hist", 0), self._compute_macd_hist)

    def ema(self, tf: str = "M1", period: int = 9) -> Optional[float]:
        """Latest EMA value. M1 → local; H1+ → semantic."""
        if tf != "M1":
            return self._semantic_indicator(f"ema_{period}")
        return self._tick_cached(("M1", "ema", period), self._compute_ema, period)

    def atr(self, tf: str = "M1", period: int = 14) -> Optional[float]:
        """Latest ATR value (price units). M1 → local; H1+ → semantic."""
        if tf != "M1":
            return self._semantic_indicator("atr")
        return self._tick_cached(("M1", "atr", period), self._compute_atr, period)

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
