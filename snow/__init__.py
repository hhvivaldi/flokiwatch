"""Snow — event-driven trade execution agent (FLO-347).

Companion to Floki (the sole trading decisor). Snow watches markets
on a 5-second cadence and fires pre-committed contingency plans written
by Floki. Paradigm: "decide once, pre-commit plans, let Snow execute."

Package entry points (populated as implementation phases ship per
`data/_design/FLO-347_Snow_RFC_v1.md` §15.2):

  snow.schema      — Pydantic v2 models (Plan, Contingency, Condition, Action)
  snow.validator   — submit-time business-rule validation
  snow.priority    — effective_priority formula + resolver          (Phase 4)
  snow.db          — CRUD for snow_plans/triggers/evaluations       (Phase 2)
  snow.live_data   — fresh MT5 ticks + indicator recompute          (Phase 3)
  snow.evaluator.* — one module per condition primitive             (Phase 3)
  snow.actions     — action dispatch under executor_lock            (Phase 5)
  snow.snow_loop   — SnowLoop.run_forever entry point               (Phase 4)
  snow.recovery    — startup reconciliation                         (Phase 4)
  snow.shadow      — shadow-mode intent logging                     (Phase 8)

Phase 1 (this commit) delivers: schema + validator + tests.
FLO-348 (commit d2d0ed8, validated 2026-04-24) is the prerequisite
thread-safety layer that FLO-347 Phase 2+ builds on.
"""

__version__ = "0.1.0"
SCHEMA_VERSION = 3  # bumped on any breaking schema change
# v1 → v2 (FLO-359 Phase 8b commit 1): adds `state_cache_json` column to
# snow_plans + introduces the stateful primitive class (indicator_crossover,
# indicator_was, price_crossed_level — land in commits 3-5). v1 plans
# remain valid (validator accepts schema_version ∈ {1, 2}); the validator
# only rejects v1 plans that *reference* a stateful primitive.
