# FLO-415 (draft) — Real-data integration test gate

**Title:** Real-data integration test gate — capture dp snapshot + exercise every Snow primitive
**Team:** Floki Watch
**Priority:** Urgent (1)
**Labels:** enhancement

## Problem

Today (2026-04-30) seven P0 bugs shipped despite 1328 passing unit tests. All same failure mode: mock fixtures hand-built dicts in the shape the test author expected Brain to publish; reality differed. Producer/consumer drift at a seam mocks elide by construction.

The 8 commits: b247a88, 6fce88c, 1fdd194, d839e5b, d0d3162, 64be6df, 1feb3d8, 7fafed8 — four production restarts in one day, all avoidable.

## Required deliverables

### 1. Real-dp snapshot capture (~10 lines in main.py)

End of `_analysis_cycle()`: atomic-write `_last_agent_data` to `data/_test_snapshots/dp_snapshot_latest.json` (temp + os.replace, try/except non-blocking). Overwrite each cycle.

### 2. Real-data primitive integration test

New file: `snow/tests/snow_integration_real_data_test.py`. Module-level `pytest.mark.skipif` when the snapshot file is missing — CI / fresh checkouts skip safely.

Parametrized assertions: every Snow primitive accessor in `live_data` returns NOT-None and raises NO exception against the real snapshot:
- `rsi` on M5/M15/H1/H4/D1
- `atr(period=14)` on M5/M15/H1/H4/D1
- `ema(period=9/21/50/200)` on M5/M15/H1/H4/D1 — 20 combinations
- `macd_histogram` on M5/M15/H1/H4/D1
- `bollinger(H1)` — verify `position` and `squeeze` keys
- `stochastic(H1)` returns float
- `macd_divergence(H1)` returns dict with `detected` key
- `pivot_points()` has `classic` or `fibonacci`
- `dp.sr_zones` is non-empty list with `price` (numeric) + `zone_type` (string) per zone

Failure messages name the exact expected field path so a regression debug is one log line away.

### 3. Synthetic-plan end-to-end test

Build a `Plan` covering every primitive at least once. Run `evaluate_condition` on each. Assert each returns `bool` — no AttributeError, no KeyError, no None.

### 4. Workflow

- DEV runs bot for one cycle locally to populate snapshot
- DEV runs `pytest snow/tests/snow_integration_real_data_test.py` before any push touching `main.py` dp rebuild, `snow/live_data.py`, `snow/evaluators/`, or `agent_data_builder.py`
- CI skips (no snapshot present)
- `.gitignore` excludes `data/_test_snapshots/` (broker prices in real dp may be sensitive)

### 5. CLAUDE.md Rule 24 (proposed)

> **Rule 24 — Real-Data Integration Tests Before Producer/Consumer Changes.** Any change to `main.py`'s `dp` rebuild block, `snow/live_data.py`, `snow/evaluators/`, or `agent_data_builder.py` must run `pytest snow/tests/snow_integration_real_data_test.py` against a fresh local snapshot BEFORE push. Mock-fixture-only tests cannot detect producer/consumer field-shape mismatches; the 2026-04-30 7-P0 incident is the canonical evidence.

## Success criteria

None of the 7 bugs from 2026-04-30 could have been pushed without the integration test catching them. CEO can verify by reverting any of the 7 fixes locally and watching the test fail with a clear "field X missing at path Y" assertion.

## Out of scope

- Curated broker-anonymized snapshot for CI runs (separate ticket if wanted)
- Typed Pydantic schema for `dp.indicators` — longer-term root fix; integration test is the cheaper short-term gate

## Priority

URGENT — methodology fix. Block all feature work tomorrow until this lands. Until this exists, every restart is a coin flip on whether just-pushed code actually runs.

## References

- main.py:1715-2238 (dp rebuild — producer)
- snow/live_data.py (consumer accessors)
- snow/evaluators/
- agent_data_builder.py (intel_feed.sr_zones path)
- 2026-04-30 commits: b247a88, 6fce88c, 1fdd194, d839e5b, d0d3162, 64be6df, 1feb3d8, 7fafed8
