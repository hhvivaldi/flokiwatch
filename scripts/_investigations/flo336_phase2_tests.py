"""
FLO-336 Phase 2 — Rule 20 test suite (5 tests).

Run from repo root:
  PYTHONIOENCODING=utf-8 python scripts/_investigations/flo336_phase2_tests.py

No pytest — standalone asserts per CLAUDE.md convention.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(r"C:/Users/Hermano/OneDrive/Desktop/XAUUSD")
os.chdir(REPO)
sys.path.insert(0, str(REPO))

from trade_lessons import _build_bucket_key, _generate_lesson_text, get_relevant_lessons

FORBIDDEN_TERMS = [
    "CAUTION",
    "DANGER",
    "correlation_break",
    "AVOID:",
    "PREFERRED:",
    "NEUTRAL:",
]


def test_1_bucket_key_is_4_dimensional():
    """Bucket key string has exactly 4 pipe-separated dimensions (4D composite)."""
    conds = {
        "rsi_h1": 45.0,
        "volume_h1": 1200,
        "session": "NY",
        "utc_hour": 15,
        "luna_environment": "CAUTION",  # intentionally populated — must NOT reach the key
        "luna_patterns": ["dollar_gold_correlation_break"],  # same
    }
    k = _build_bucket_key("BUY", conds)
    # 4 dims = 3 pipe separators
    assert k.count(" | ") == 3, f"expected 3 pipe separators (4D), got {k.count(' | ')} in {k!r}"
    # Zero editorial dimensions
    for term in FORBIDDEN_TERMS:
        assert term not in k, f"forbidden term {term!r} found in bucket key: {k!r}"
    print(f"  OK  bucket key (4D): {k!r}")


def test_2_sample_trade_conditions_file_buckets_correctly():
    """Real trade_conditions file produces expected 4D bucket without editorial terms."""
    sample_file = REPO / "data" / "trade_conditions" / "1606607186.json"
    assert sample_file.exists(), "expected sample file missing"
    with sample_file.open("r", encoding="utf-8") as f:
        data = json.load(f)
    conds = data.get("conditions_at_open", {}) or {}
    direction = data.get("direction", "BUY")
    k = _build_bucket_key(direction, conds)
    assert k.count(" | ") == 3, f"expected 4D, got {k.count(' | ')} in {k!r}"
    for term in FORBIDDEN_TERMS:
        assert term not in k, f"forbidden {term!r} in {k!r}"
    print(f"  OK  real file bucket: {k!r}")


def test_3_lesson_text_zero_forbidden_terms():
    """_generate_lesson_text output contains zero CAUTION/DANGER/correlation_break/AVOID:/PREFERRED:/NEUTRAL: hits."""
    cases = [
        # (wins, losses, avg_pnl, expected: factual, no prefixes)
        (1, 2, -2.54),      # low WR — was AVOID
        (8, 2, 15.2),       # high WR — was PREFERRED
        (5, 5, 0.3),        # mid WR — was NEUTRAL
        (1, 1, -0.5),       # insufficient — was NEUTRAL with "insufficient data"
    ]
    bucket_key = "BUY | RSI WEAK | Vol UNKNOWN | NY"  # already 4D
    for wins, losses, avg in cases:
        out = _generate_lesson_text(bucket_key, wins, losses, avg)
        for term in FORBIDDEN_TERMS:
            assert term not in out, f"forbidden {term!r} in lesson output: {out!r}"
        print(f"  OK  ({wins}W/{losses}L avg={avg}): {out!r}")


def test_4_lesson_text_contains_factual_data():
    """Output must still contain wins/losses/avg_pnl numbers — Floki needs the stats."""
    bucket_key = "SELL | RSI WEAK | Vol LOW | LONDON"
    out = _generate_lesson_text(bucket_key, 0, 6, -18.83)
    # Must contain core factual indicators
    assert "6" in out, f"total count missing from {out!r}"
    assert "0" in out, f"win count missing from {out!r}"
    assert "-18.83" in out or "-$18.83" in out, f"avg_pnl missing from {out!r}"
    assert bucket_key in out, f"bucket_key missing from {out!r}"
    print(f"  OK  factual: {out!r}")


def test_5_end_to_end_get_relevant_lessons_zero_forbidden():
    """Full pipeline: get_relevant_lessons() returns lessons with no forbidden terms.

    Since LESSONS_CURRENT_ERA_SHAS whitelist currently excludes everything (FLO-334
    Phase 2 hasn't shipped), we expect 0 lessons. Verify the return is empty AND
    that if we bypass the era filter, the computed output is still clean.
    """
    import config as _cfg
    orig = list(getattr(_cfg, "LESSONS_CURRENT_ERA_SHAS", []))
    # Widen the filter temporarily to include today's SHAs — verifies clean output end-to-end
    widened = orig + ["ecb9ac8", "dea48bd", "390a03a", "cea2447", "a866c7b",
                     "2982d24", "56fd4eb", "1442b3e", "5f721ad", "32f26cd",
                     "3186114", "0e1481f", "4734efc"]
    _cfg.LESSONS_CURRENT_ERA_SHAS = widened
    try:
        lessons = get_relevant_lessons(min_occurrences=2, limit=20)
        for les in lessons:
            bucket_str = str(les.get("bucket", ""))
            lesson_str = str(les.get("lesson", ""))
            for term in FORBIDDEN_TERMS:
                assert term not in bucket_str, f"forbidden {term!r} in bucket: {bucket_str!r}"
                assert term not in lesson_str, f"forbidden {term!r} in lesson: {lesson_str!r}"
        print(f"  OK  {len(lessons)} lessons returned, all clean")
        if lessons:
            print(f"      sample: {lessons[0]}")
    finally:
        _cfg.LESSONS_CURRENT_ERA_SHAS = orig


def main() -> int:
    tests = [
        ("1. bucket key is 4D", test_1_bucket_key_is_4_dimensional),
        ("2. real sample file buckets correctly", test_2_sample_trade_conditions_file_buckets_correctly),
        ("3. lesson text has zero forbidden terms", test_3_lesson_text_zero_forbidden_terms),
        ("4. lesson text contains factual data", test_4_lesson_text_contains_factual_data),
        ("5. end-to-end get_relevant_lessons clean", test_5_end_to_end_get_relevant_lessons_zero_forbidden),
    ]
    failed = 0
    for name, fn in tests:
        print(f"TEST {name}")
        try:
            fn()
        except AssertionError as e:
            print(f"  FAIL  {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR  {type(e).__name__}: {e}")
            failed += 1
    print()
    print(f"Results: {len(tests) - failed}/{len(tests)} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
