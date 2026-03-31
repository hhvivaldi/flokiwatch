# Gemini 3 Flash Migration Log

This document summarizes the March 18, 2026 migration of FlokiWatch’s decision engine from the legacy Claude/local/shadow architecture to **Gemini 3 Flash** with native tool calling.

## Summary (March 18, 2026)
- FlokiWatch AI decision engine migrated to **Gemini 3 Flash** using the `google-genai` SDK with native function calling.
- Legacy local/shadow model path removed.
- Rex validator remains **OpenAI GPT-4o only**.
- Bot cadence aligned to **5-minute** agent/analysis cycles (`FLOKI_CALL_INTERVAL=300`).
- MT5 remains source of truth for open positions; OPEN is blocked if any position already exists.

## Why this change
- Reduce latency and improve tool-call throughput vs Claude.
- Simplify architecture by removing local/shadow execution paths.
- Improve reliability by keeping a single authoritative decision path.

## Implementation Notes
### Phase 1 — Cleanup
- Removed local/shadow model execution path.
- Deleted `shadow_model.py`.
- Removed shadow/local config flags.
- Removed temporary diagnostics (kept operational logs like `BALANCE_CAPTURE`, `BALANCE_DIFF`, `DEAL_REFRESH`, `WATCH_REMAP`, `SIMBA_WAKE`).
- Removed Gemini residue from `rex_validator.py` so Rex remains GPT-4o.

### Phase 2 — Gemini integration
- `ai_agent.py` migrated to Gemini native tool calling:
  - Tools are declared as Gemini `function_declarations`.
  - Tool loop executes `function_call` → tool execution → `function_response` until final text.
- `config.py` now provides:
  - `FLOKI_MODEL` default: `gemini-3-flash-preview`
  - `FLOKI_CALL_INTERVAL` default: `300`
  - `ANALYSIS_INTERVAL_SECONDS` aligned to `FLOKI_CALL_INTERVAL`.

### Phase 3 — Safety
- Pre-flight OPEN guard in `execute_agent_trade()`:
  - If MT5 already has any open position, OPEN is rejected.

## Commits
- Phase 1 cleanup: `183fae7`
- Phase 2/3 (Gemini + 5m cadence): `00e7f8c`
- Schema sanitization + remove Anthropic fast path: `5c577ca`
- Temporary diagnostics added then removed: `8b3a968` → `a7a9e5c`

## Current system state
- **Agent:** Gemini 3 Flash (`FLOKI_MODEL=gemini-3-flash-preview`)
- **Debate (Rex):** OpenAI GPT-4o only
- **Cadence:** 5-minute analysis/agent cycle
- **Execution:** MT5 + EA Bridge for tick-by-tick management; Python remains non-blocking.

## Known follow-ups
- Consider using Gemini `system_instruction` rather than injecting system prompt as a user message (optimization).
- Monitor ongoing token/cost usage and tool-call patterns.
- Continue validating behavior under 5-minute cadence.
