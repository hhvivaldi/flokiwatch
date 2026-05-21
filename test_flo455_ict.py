"""FLO-455 Phase 1 — ICT zone payload builder tests.

Tests MY extraction/filtering/JSON-mapping logic deterministically by mocking the
smc.* outputs (the smartmoneyconcepts BETA detection itself is validated live on
the MT5 chart per the ticket). Standalone. Run: python test_flo455_ict.py
"""
import json
import os
import tempfile

import numpy as np
import pandas as pd

import ict_zones as I


def _ohlc(n=60):
    idx = pd.date_range("2026-05-01", periods=n, freq="h", tz="UTC")
    base = np.linspace(3200, 3260, n)
    return pd.DataFrame({"open": base, "high": base + 1, "low": base - 1,
                         "close": base + 0.5, "volume": np.full(n, 1000.0)}, index=idx)


class _FakeSMC:
    """Crafted smc outputs: one unmitigated + one mitigated FVG, one OB, one sweep."""
    def __init__(self, idx):
        self.idx = idx
    def swing_highs_lows(self, ohlc, swing_length=50):
        return pd.DataFrame({"HighLow": [1], "Level": [3250.0]}, index=[self.idx[50]])
    def fvg(self, ohlc):
        return pd.DataFrame(
            {"FVG": [1, -1], "Top": [3250.0, 3260.0], "Bottom": [3245.0, 3255.0],
             "MitigatedIndex": [np.nan, 10]},  # 2nd is mitigated -> excluded
            index=[self.idx[55], self.idx[56]])
    def ob(self, ohlc, swings):
        return pd.DataFrame(
            {"OB": [1], "Top": [3240.0], "Bottom": [3235.0], "OBVolume": [1000.0],
             "MitigatedIndex": [np.nan], "Percentage": [50.0]}, index=[self.idx[40]])
    def liquidity(self, ohlc, swings):
        return pd.DataFrame(
            {"Liquidity": [-1], "Level": [3220.0], "End": [np.nan], "Swept": [0]},
            index=[self.idx[30]])


def test_extraction_and_filtering():
    df = _ohlc()
    orig_smc, orig_ok = I.smc, I._SMC_OK
    I.smc, I._SMC_OK = _FakeSMC(df.index), True
    try:
        p = I.build_ict_zones_payload(df, "H1")
    finally:
        I.smc, I._SMC_OK = orig_smc, orig_ok
    types = sorted(z["type"] for z in p["zones"])
    assert "timestamp" in p and isinstance(p["zones"], list)
    assert types == ["FVG", "OB", "SWEEP"], f"expected one of each (mitigated FVG excluded): {types}"
    fvg = next(z for z in p["zones"] if z["type"] == "FVG")
    assert fvg["direction"] == "bullish" and fvg["top"] == 3250.0 and fvg["bottom"] == 3245.0
    assert fvg["status"] == "unmitigated" and fvg["timeframe"] == "H1" and fvg["candle_time"]
    sweep = next(z for z in p["zones"] if z["type"] == "SWEEP")
    assert sweep["direction"] == "low" and sweep["level"] == 3220.0 and "top" not in sweep
    ob = next(z for z in p["zones"] if z["type"] == "OB")
    assert ob["direction"] == "bullish" and ob["top"] > ob["bottom"]
    print("PASS test_extraction_and_filtering (1 OB + 1 FVG + 1 SWEEP; mitigated FVG filtered out)")


def test_failsoft_edges():
    assert I.build_ict_zones_payload(None, "H1")["zones"] == []
    assert I.build_ict_zones_payload(_ohlc(5), "H1")["zones"] == []  # < swing_length+5
    p = I.build_ict_zones_payload(_ohlc(5), "H1")
    assert "timestamp" in p and isinstance(p["zones"], list)
    print("PASS test_failsoft_edges (None / short df -> empty, no crash)")


def test_write_json_atomic_and_valid():
    df = _ohlc()
    orig_smc, orig_ok = I.smc, I._SMC_OK
    I.smc, I._SMC_OK = _FakeSMC(df.index), True
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "ict_zones.json")
        I._write_json(I.build_ict_zones_payload(df, "H1"), path)
        with open(path, encoding="utf-8") as f:
            loaded = json.load(f)
    I.smc, I._SMC_OK = orig_smc, orig_ok
    assert loaded["zones"] and all("type" in z for z in loaded["zones"])
    print("PASS test_write_json_atomic_and_valid (parseable JSON, schema intact)")


if __name__ == "__main__":
    test_extraction_and_filtering()
    test_failsoft_edges()
    test_write_json_atomic_and_valid()
    print("\nALL FLO-455 PHASE 1 TESTS PASSED")
