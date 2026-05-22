"""FLO-455 Phase 1 — ICT zone payload builder tests (Option A: Floki's own
FLO-438 detectors as the data source; the smartmoneyconcepts package was dropped
because it returns 0 zones at gold-scale prices).

Tests the pure mapping from _scan_fvgs/_scan_sweeps output -> ict_zones.json
schema. Standalone. Run: python test_flo455_ict.py
"""
import json
import os
import tempfile

import ict_zones as I

# Sample FLO-438 detector output shapes.
_FVGS = [
    {"direction": "bullish", "top": 4520.5, "bottom": 4515.2, "midpoint": 4517.8,
     "size_pips": 53.0, "timeframe": "H1", "age_candles": 3, "filled_pct": 0.0,
     "formed_at_iso": "2026-05-22T04:00:00Z"},
    {"direction": "bearish", "top": 4560.8, "bottom": 4555.1, "midpoint": 4557.9,
     "size_pips": 57.0, "timeframe": "H1", "age_candles": 8, "filled_pct": 20.0,
     "formed_at_iso": "2026-05-22T00:00:00Z"},
]
_SWEEPS = [
    {"level": 4575.0, "direction": "BSL", "sweep_candle_time_iso": "2026-05-22T05:00:00Z",
     "wick_size_pips": 30.0, "recovered_pct": 80.0, "age_candles": 2, "timeframe": "H1"},
    {"level": 4505.0, "direction": "SSL", "sweep_candle_time_iso": "2026-05-22T02:00:00Z",
     "wick_size_pips": 25.0, "recovered_pct": 60.0, "age_candles": 5, "timeframe": "H1"},
]


def test_mapping_to_schema():
    p = I.build_ict_zones_payload(_FVGS, _SWEEPS, "H1")
    assert "timestamp" in p and isinstance(p["zones"], list)
    assert len(p["zones"]) == 4
    fvg = p["zones"][0]
    assert fvg == {"type": "FVG", "direction": "bullish", "timeframe": "H1",
                   "top": 4520.5, "bottom": 4515.2, "status": "unmitigated",
                   "candle_time": "2026-05-22T04:00:00Z"}, fvg
    sweeps = [z for z in p["zones"] if z["type"] == "SWEEP"]
    bsl = next(z for z in sweeps if z["level"] == 4575.0)
    ssl = next(z for z in sweeps if z["level"] == 4505.0)
    assert bsl["direction"] == "high" and "top" not in bsl, bsl   # BSL -> high
    assert ssl["direction"] == "low", ssl                          # SSL -> low
    assert bsl["candle_time"] == "2026-05-22T05:00:00Z"
    print("PASS test_mapping_to_schema (2 FVG + 2 SWEEP; BSL->high, SSL->low)")


def test_failsoft_empty():
    assert I.build_ict_zones_payload(None, None, "H1")["zones"] == []
    assert I.build_ict_zones_payload([], [], "H1")["zones"] == []
    # rows missing required fields are skipped, not crashed
    bad = I.build_ict_zones_payload([{"direction": "bullish"}], [{"direction": "BSL"}], "H1")
    assert bad["zones"] == [], bad
    print("PASS test_failsoft_empty (None/[]/malformed -> empty, no crash)")


def test_write_json_atomic_and_valid():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "ict_zones.json")
        I._write_json(I.build_ict_zones_payload(_FVGS, _SWEEPS, "H1"), path)
        with open(path, encoding="utf-8") as f:
            loaded = json.load(f)
    assert len(loaded["zones"]) == 4 and all("type" in z for z in loaded["zones"])
    print("PASS test_write_json_atomic_and_valid")


if __name__ == "__main__":
    test_mapping_to_schema()
    test_failsoft_empty()
    test_write_json_atomic_and_valid()
    print("\nALL FLO-455 PHASE 1 TESTS PASSED (Option A — Floki's own detectors)")
