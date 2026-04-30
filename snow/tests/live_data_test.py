"""LiveData tests — Phase 3a infrastructure.

Covers:
  * refresh() happy path + 4 failure modes (disconnect, None rates,
    empty rates, MT5 raises)
  * price() bid/ask/mid + None on disconnect
  * Tick-cache memoisation (compute once per tick per key)
  * Per-indicator 4-case matrix per advisor item #6:
      happy path  +  None rates  +  0-row array  +  too few bars
  * Fidelity check (CRITICAL, advisor #5): LiveData indicators match
    technical_analyzer.calculate_indicators element-wise
  * H1+ delegation to SemanticCache
  * Rule 23: no raw MetaTrader5 import in the module source

All tests monkeypatch `snow.live_data.mt5` with a synthetic fake that
honours the attributes LiveData actually calls (`symbol_info_tick`,
`copy_rates_from_pos`, `TIMEFRAME_M1`). This keeps tests portable and
deterministic — no broker involvement.
"""
from __future__ import annotations

import math
import numpy as np
import pandas as pd
import pytest
from types import SimpleNamespace
from typing import Optional

from snow import live_data as live_data_module
from snow.live_data import LiveData, M1_BAR_COUNT, MIN_BARS_FOR_INDICATOR
from snow.semantic_cache import SemanticCache


# =============================================================================
# Helpers
# =============================================================================

_OHLC_DTYPE = np.dtype([
    ("time", "i8"), ("open", "f8"), ("high", "f8"),
    ("low", "f8"), ("close", "f8"),
    ("tick_volume", "i8"), ("spread", "i4"), ("real_volume", "i8"),
])


def _make_ohlc(n_bars: int, seed: int = 42) -> np.ndarray:
    """Deterministic OHLC array shaped like an MT5 `copy_rates_from_pos`
    return: structured ndarray with fields time, open, high, low, close,
    tick_volume, spread, real_volume.

    `n_bars=0` returns an empty structured array with the right dtype —
    matches what MT5 returns for a valid symbol with no bars in the
    requested window.
    """
    arr = np.zeros(n_bars, dtype=_OHLC_DTYPE)
    if n_bars == 0:
        return arr

    rng = np.random.default_rng(seed)
    steps = rng.normal(0, 1.0, size=n_bars).cumsum()
    close = 4700.0 + steps
    open_ = np.concatenate(([close[0]], close[:-1]))
    high = np.maximum(open_, close) + rng.uniform(0.1, 1.5, size=n_bars)
    low = np.minimum(open_, close) - rng.uniform(0.1, 1.5, size=n_bars)
    times = np.arange(n_bars, dtype=np.int64) * 60  # unix seconds

    arr["time"] = times
    arr["open"] = open_
    arr["high"] = high
    arr["low"] = low
    arr["close"] = close
    arr["tick_volume"] = 100
    arr["spread"] = 20
    arr["real_volume"] = 0
    return arr


class _FakeMT5:
    """Stand-in for the mt5_safe proxy in tests. Configurable per scenario."""

    TIMEFRAME_M1 = 1

    def __init__(
        self,
        *,
        tick=None,
        rates=None,
        raise_on: Optional[str] = None,
    ):
        self._tick = tick
        self._rates = rates
        self._raise_on = raise_on

    def symbol_info_tick(self, symbol):
        if self._raise_on == "tick":
            raise RuntimeError("simulated MT5 tick failure")
        return self._tick

    def copy_rates_from_pos(self, symbol, tf, start, count):
        if self._raise_on == "rates":
            raise RuntimeError("simulated MT5 rates failure")
        return self._rates


def _make_tick(bid: float, ask: float):
    return SimpleNamespace(bid=bid, ask=ask, time=1_700_000_000)


@pytest.fixture
def empty_semantic():
    """SemanticCache that always returns None — removes semantic as a
    variable in live-data tests."""
    return SemanticCache(lambda: None)


@pytest.fixture
def live(monkeypatch, empty_semantic):
    """LiveData factory: returns (ld, fake) where `fake` is the FakeMT5
    instance installed into snow.live_data. Tests configure `fake.tick`
    / `fake.rates` / `fake._raise_on` before calling `ld.refresh()`."""
    def _make(tick=None, rates=None, raise_on=None) -> tuple[LiveData, _FakeMT5]:
        fake = _FakeMT5(tick=tick, rates=rates, raise_on=raise_on)
        monkeypatch.setattr(live_data_module, "mt5", fake)
        return LiveData("XAUUSD", empty_semantic), fake
    return _make


# =============================================================================
# refresh() lifecycle + failure modes
# =============================================================================

class TestRefresh:

    def test_happy_path_populates_tick_and_bars(self, live):
        rates = _make_ohlc(120)
        ld, _ = live(tick=_make_tick(4700.5, 4701.0), rates=rates)
        ld.refresh()
        assert ld.price() is not None
        assert ld._m1_bars is not None
        assert len(ld._m1_bars) == 120

    def test_disconnected_tick_returns_none_rates_ok(self, live):
        # MT5 connected but tick unavailable
        rates = _make_ohlc(120)
        ld, _ = live(tick=None, rates=rates)
        ld.refresh()
        assert ld.price() is None
        assert ld._m1_bars is not None  # bars fetched separately

    def test_rates_none_keeps_bars_none(self, live):
        ld, _ = live(tick=_make_tick(4700, 4701), rates=None)
        ld.refresh()
        assert ld._m1_bars is None

    def test_empty_rates_array_keeps_bars_none(self, live):
        empty = _make_ohlc(0)
        ld, _ = live(tick=_make_tick(4700, 4701), rates=empty)
        ld.refresh()
        assert ld._m1_bars is None

    def test_tick_call_raises_fails_silent(self, live):
        ld, _ = live(
            tick=_make_tick(4700, 4701),
            rates=_make_ohlc(120),
            raise_on="tick",
        )
        ld.refresh()  # must not raise
        assert ld.price() is None
        assert ld._m1_bars is None  # atomic: bars cleared too

    def test_rates_call_raises_fails_silent(self, live):
        ld, _ = live(
            tick=_make_tick(4700, 4701),
            rates=_make_ohlc(120),
            raise_on="rates",
        )
        ld.refresh()
        assert ld.price() is None
        assert ld._m1_bars is None

    def test_refresh_clears_indicator_cache_first(self, live):
        """If the fetch raises, stale cached values from the previous
        tick must NOT leak into this tick's accessor calls."""
        ld, fake = live(
            tick=_make_tick(4700, 4701),
            rates=_make_ohlc(120),
        )
        ld.refresh()
        _ = ld.rsi()  # populates cache
        assert ld._indicator_cache  # non-empty

        # Next tick: MT5 fails
        fake._raise_on = "tick"
        ld.refresh()
        assert ld._indicator_cache == {}  # cleared first, then fetch failed
        assert ld.rsi() is None


# =============================================================================
# price()
# =============================================================================

class TestPrice:

    def test_bid(self, live):
        ld, _ = live(tick=_make_tick(4700.0, 4701.0), rates=_make_ohlc(120))
        ld.refresh()
        assert ld.price("bid") == 4700.0

    def test_ask(self, live):
        ld, _ = live(tick=_make_tick(4700.0, 4701.0), rates=_make_ohlc(120))
        ld.refresh()
        assert ld.price("ask") == 4701.0

    def test_mid_default(self, live):
        ld, _ = live(tick=_make_tick(4700.0, 4702.0), rates=_make_ohlc(120))
        ld.refresh()
        assert ld.price() == 4701.0

    def test_disconnected_returns_none(self, live):
        ld, _ = live(tick=None, rates=_make_ohlc(120))
        ld.refresh()
        assert ld.price() is None

    def test_unknown_side_returns_none(self, live):
        ld, _ = live(tick=_make_tick(4700, 4701), rates=_make_ohlc(120))
        ld.refresh()
        assert ld.price("bogus") is None


# =============================================================================
# Tick-cache memoisation
# =============================================================================

class TestTickCache:

    def test_rsi_computed_once_per_tick(self, live):
        ld, _ = live(
            tick=_make_tick(4700, 4701), rates=_make_ohlc(120)
        )
        ld.refresh()
        v1 = ld.rsi()
        v2 = ld.rsi()
        v3 = ld.rsi()
        assert v1 == v2 == v3
        # Second refresh → new cache
        ld.refresh()
        v4 = ld.rsi()
        assert v4 is not None

    def test_different_periods_cached_separately(self, live):
        ld, _ = live(
            tick=_make_tick(4700, 4701), rates=_make_ohlc(120)
        )
        ld.refresh()
        a = ld.rsi(period=14)
        b = ld.rsi(period=7)
        # Both computed and cached under distinct keys
        assert ("M1", "rsi", 14) in ld._indicator_cache
        assert ("M1", "rsi", 7) in ld._indicator_cache


# =============================================================================
# Indicator degenerate cases (happy + None + 0-row + too-few-bars)
# =============================================================================

@pytest.mark.parametrize("method_name,period_kwargs", [
    ("rsi", {"period": 14}),
    ("macd_histogram", {}),
    ("ema", {"period": 9}),
    ("atr", {"period": 14}),
])
class TestIndicatorDegenerate:

    def test_happy_path_returns_float(self, live, method_name, period_kwargs):
        ld, _ = live(
            tick=_make_tick(4700, 4701), rates=_make_ohlc(120)
        )
        ld.refresh()
        val = getattr(ld, method_name)(**period_kwargs)
        assert val is not None
        assert isinstance(val, float)
        assert math.isfinite(val)

    def test_rates_none_returns_none(self, live, method_name, period_kwargs):
        ld, _ = live(tick=_make_tick(4700, 4701), rates=None)
        ld.refresh()
        assert getattr(ld, method_name)(**period_kwargs) is None

    def test_zero_rows_returns_none(self, live, method_name, period_kwargs):
        ld, _ = live(
            tick=_make_tick(4700, 4701), rates=_make_ohlc(0)
        )
        ld.refresh()
        assert getattr(ld, method_name)(**period_kwargs) is None

    def test_too_few_bars_returns_none(self, live, method_name, period_kwargs):
        # MIN_BARS_FOR_INDICATOR - 1 bars is below the floor
        ld, _ = live(
            tick=_make_tick(4700, 4701),
            rates=_make_ohlc(MIN_BARS_FOR_INDICATOR - 1),
        )
        ld.refresh()
        assert getattr(ld, method_name)(**period_kwargs) is None


# =============================================================================
# Fidelity — LiveData must match technical_analyzer.py byte-for-byte
# =============================================================================

class TestFidelityVsTechnicalAnalyzer:
    """RFC §14.3 item 3: Snow's indicator computation must not drift from
    Brain's. A single test fed the same deterministic OHLC dataset into
    both paths and asserts the last-row values agree within 1e-9. If
    anyone future-edits either formula, this fails loudly."""

    @pytest.fixture
    def shared_df(self) -> pd.DataFrame:
        rates = _make_ohlc(120, seed=1337)
        return pd.DataFrame(rates)

    def _brain_indicators(self, df: pd.DataFrame) -> dict:
        """Route df through technical_analyzer.calculate_indicators and
        return the last row's scalar indicator values."""
        from technical_analyzer import calculate_indicators
        out = calculate_indicators(df.copy())
        last = out.iloc[-1]
        return {
            "rsi": float(last["rsi_14"]),
            "ema_9": float(last["ema_9"]),
            "macd_hist": float(last["macd_hist"]),
            "atr": float(last["atr_14"]),
        }

    def test_rsi_matches_brain(self, live, shared_df):
        ld, _ = live(
            tick=_make_tick(4700, 4701), rates=shared_df.to_records(index=False),
        )
        ld.refresh()
        brain = self._brain_indicators(shared_df)
        assert ld.rsi() == pytest.approx(brain["rsi"], abs=1e-9)

    def test_ema_matches_brain(self, live, shared_df):
        ld, _ = live(
            tick=_make_tick(4700, 4701), rates=shared_df.to_records(index=False),
        )
        ld.refresh()
        brain = self._brain_indicators(shared_df)
        assert ld.ema(period=9) == pytest.approx(brain["ema_9"], abs=1e-9)

    def test_macd_hist_matches_brain(self, live, shared_df):
        ld, _ = live(
            tick=_make_tick(4700, 4701), rates=shared_df.to_records(index=False),
        )
        ld.refresh()
        brain = self._brain_indicators(shared_df)
        assert ld.macd_histogram() == pytest.approx(
            brain["macd_hist"], abs=1e-9
        )

    def test_atr_matches_brain(self, live, shared_df):
        ld, _ = live(
            tick=_make_tick(4700, 4701), rates=shared_df.to_records(index=False),
        )
        ld.refresh()
        brain = self._brain_indicators(shared_df)
        assert ld.atr(period=14) == pytest.approx(brain["atr"], abs=1e-9)


# =============================================================================
# H1+ delegation to SemanticCache
# =============================================================================

class TestSemanticDelegation:

    def _live_with_semantic(self, monkeypatch, semantic_data):
        fake = _FakeMT5(
            tick=_make_tick(4700, 4701), rates=_make_ohlc(120)
        )
        monkeypatch.setattr(live_data_module, "mt5", fake)
        cache = SemanticCache(lambda: semantic_data)
        ld = LiveData("XAUUSD", cache)
        ld.refresh()
        return ld

    def test_h1_rsi_reads_per_tf(self, monkeypatch):
        """FLO-410: non-M1 reads come from dp.multi_tf_indicators[tf],
        NOT the flat dp.indicators path. Old test asserted the latter
        (which was the bug — TF-agnostic single-TF read). New contract:
        H1 RSI must come from multi_tf_indicators.H1.rsi."""
        ld = self._live_with_semantic(
            monkeypatch,
            {"multi_tf_indicators": {"H1": {"rsi": 68.3}}},
        )
        assert ld.rsi(tf="H1") == 68.3

    def test_h4_macd_hist_reads_per_tf(self, monkeypatch):
        """FLO-410: macd_histogram reads mtf[tf].macd.histogram."""
        ld = self._live_with_semantic(
            monkeypatch,
            {"multi_tf_indicators": {
                "H4": {"macd": {"histogram": 0.25}},
            }},
        )
        assert ld.macd_histogram(tf="H4") == 0.25

    def test_h1_ema_reads_per_tf(self, monkeypatch):
        """FLO-410: ema reads mtf[tf].ema<period> (no underscore)."""
        ld = self._live_with_semantic(
            monkeypatch,
            {"multi_tf_indicators": {
                "H1": {"ema50": 4710.5},
            }},
        )
        assert ld.ema(tf="H1", period=50) == 4710.5

    def test_semantic_missing_returns_none(self, monkeypatch):
        ld = self._live_with_semantic(monkeypatch, {"indicators": {}})
        assert ld.rsi(tf="H1") is None

    def test_semantic_entire_cache_empty_returns_none(self, monkeypatch):
        ld = self._live_with_semantic(monkeypatch, None)
        assert ld.rsi(tf="H1") is None

    # FLO-410: per-TF correctness — different TFs return different values.

    def test_per_tf_rsi_independence(self, monkeypatch):
        """rsi(M5) and rsi(H1) must return DIFFERENT values when the
        per-TF cache has different per-TF data. Pre-fix this test
        would have failed (both returned the same flat value)."""
        ld = self._live_with_semantic(
            monkeypatch,
            {"multi_tf_indicators": {
                "M5": {"rsi": 35.0},
                "M15": {"rsi": 50.0},
                "H1": {"rsi": 70.0},
                "H4": {"rsi": 80.0},
            }},
        )
        assert ld.rsi(tf="M5") == 35.0
        assert ld.rsi(tf="M15") == 50.0
        assert ld.rsi(tf="H1") == 70.0
        assert ld.rsi(tf="H4") == 80.0

    def test_per_tf_ema_alignment_independence(self, monkeypatch):
        """All four periods must read from the same TF block,
        independent of other TFs."""
        ld = self._live_with_semantic(
            monkeypatch,
            {"multi_tf_indicators": {
                "M15": {"ema9": 4632.0, "ema21": 4630.0,
                        "ema50": 4626.0, "ema200": 4616.0},
                "H1":  {"ema9": 4640.0, "ema21": 4636.0,
                        "ema50": 4630.0, "ema200": 4600.0},
            }},
        )
        # M15 alignment values
        assert ld.ema(tf="M15", period=9) == 4632.0
        assert ld.ema(tf="M15", period=21) == 4630.0
        assert ld.ema(tf="M15", period=50) == 4626.0
        assert ld.ema(tf="M15", period=200) == 4616.0
        # H1 alignment values — must NOT bleed into the M15 read
        assert ld.ema(tf="H1", period=9) == 4640.0
        assert ld.ema(tf="H1", period=200) == 4600.0

    def test_bollinger_stochastic_divergence_per_tf_resolve(self, monkeypatch):
        """FLO-411: bollinger / stochastic / macd_divergence are no
        longer H1-only. compute_indicators_from_candles publishes
        these per-TF in dp.multi_tf_indicators[tf]; the consumers route
        non-H1 reads there. Test: each accessor resolves a per-TF
        value when the cache provides it."""
        ld = self._live_with_semantic(
            monkeypatch,
            {
                "multi_tf_indicators": {
                    "M5":  {"bollinger": {"upper": 4630.0, "middle": 4625.0, "lower": 4620.0, "position": 0.7, "squeeze": False},
                            "stochastic": {"value": 65.0},
                            "macd": {"value": 0.5, "signal": 0.3, "histogram": 0.2,
                                     "divergence": {"detected": True, "type": "bullish", "bars_since": 3}}},
                    "M15": {"bollinger": {"upper": 4640.0, "middle": 4625.0, "lower": 4610.0, "position": 0.5, "squeeze": True},
                            "stochastic": {"value": 30.0},
                            "macd": {"divergence": {"detected": False, "type": None}}},
                    "H4":  {"bollinger": {"upper": 4700.0, "middle": 4625.0, "lower": 4550.0, "position": 0.3, "squeeze": False},
                            "stochastic": {"value": 75.0},
                            "macd": {"divergence": {"detected": False, "type": None}}},
                },
            },
        )
        # bollinger
        bb_m5 = ld.bollinger(tf="M5")
        assert bb_m5 is not None and bb_m5["position"] == 0.7
        bb_m15 = ld.bollinger(tf="M15")
        assert bb_m15 is not None and bb_m15["squeeze"] is True
        bb_h4 = ld.bollinger(tf="H4")
        assert bb_h4 is not None and bb_h4["position"] == 0.3
        # stochastic
        assert ld.stochastic(tf="M5") == 65.0
        assert ld.stochastic(tf="M15") == 30.0
        assert ld.stochastic(tf="H4") == 75.0
        # macd_divergence
        m5_div = ld.macd_divergence(tf="M5")
        assert m5_div is not None and m5_div["detected"] is True
        assert m5_div["type"] == "bullish"
        m15_div = ld.macd_divergence(tf="M15")
        assert m15_div is not None and m15_div["detected"] is False


# =============================================================================
# Rule 23 enforcement — static check on module source
# =============================================================================

class TestRule23:

    def test_no_raw_metatrader5_import(self):
        """snow/live_data.py must use `from mt5_safe import mt5` only.
        A raw `import MetaTrader5` would bypass the mt5_lock wrapper
        (Rule 23). Grep the file source to catch drift at review time."""
        import pathlib
        src = pathlib.Path(live_data_module.__file__).read_text(
            encoding="utf-8"
        )
        # Allow the docstring mention; forbid any actual import line.
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert not stripped.startswith("import MetaTrader5"), line
            assert not stripped.startswith("from MetaTrader5"), line
