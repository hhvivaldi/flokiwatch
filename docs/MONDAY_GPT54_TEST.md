# Monday GPT-5.4 Test Checklist

**Date:** 2026-03-29 (Sunday market open 22:00 UTC)
**Commit:** b8988ae (FLO-130 + temperature + Rex 9 tools)
**Migration:** Gemini Flash → GPT-5.4 for Floki, GPT-5 mini for Rex

---

## Tests (run after first 5-10 live cycles)

### Infrastructure

- [ ] **TEST 1** — Bot initializes: `AI Agent initialized: model=gpt-5.4`
- [ ] **TEST 2** — First cycle completes: `FLOKI_TURN` logs visible, 28 tools available
- [ ] **TEST 3** — Cost logging: `FLOKI | model=gpt-5.4 | input_tokens=X | output_tokens=Y | cost=$Z.ZZ`
- [ ] **TEST 11** — JSON valid on every cycle (no parse errors)
- [ ] **TEST 12** — Latency < 90 seconds per cycle

### Debate

- [ ] **TEST 4** — Rex cross-model debate works: GPT-5.4 Floki ↔ GPT-5 mini Rex (9 tools)
- [ ] **TEST 10** — Rex uses 3-phase (check data / interpret / help refine)

### Thesis Continuity (FLO-127/128)

- [ ] **TEST 5** — `data/active_thesis.json` populated after first cycle
- [ ] **TEST 6** — `FLOKI | previous_thesis injected` log line on cycle 2+
- [ ] **TEST 7** — Floki 3-phase thinking (see / interpret / decide) in flowing prose
- [ ] **TEST 8** — Floki references previous thesis ("thesis still holds" / "what changed")
- [ ] **TEST 9** — Anti-repetition triggers after 3+ same-thesis cycles

### Data Quality

- [ ] **TEST 13** — Floki references cross-market data (silver, dollar strength, BTC, position_in_range)

### Trade Execution

- [ ] **TEST 14** — If OPEN/CLOSE/ADJUST decision: FLOKI_FOLLOWUP fires and tool executes
- [ ] **TEST 15** — Cost tracking: estimate daily cost after first hour of trading

---

## Expected Values

| Metric | Expected |
|--------|----------|
| Model | gpt-5.4-2026-03-05 |
| Input tokens/cycle | ~8,000-12,000 |
| Output tokens/cycle | ~400-800 |
| Cost/cycle | ~$0.03-0.05 |
| Latency/cycle | 15-60 seconds |
| Daily cost (200 cycles) | ~$6-10 |
| Rex model | gpt-5-mini |
| Rex tools | 9 |
| Rex cost/debate | ~$0.002 |

## How to Run Tests

```bash
# TEST 1: Check initialization
grep "AI Agent initialized" logs/trading_bot_2026-03-29.log

# TEST 2+3: Check first cycle
grep "FLOKI_TURN\|FLOKI |.*model=\|FLOKI |.*cost=" logs/trading_bot_2026-03-29.log | head -10

# TEST 4: Check Rex debate
grep "DEBATE | turn\|REX_TOOL\|REX | tool loop" logs/trading_bot_2026-03-29.log | head -10

# TEST 5: Check thesis file
cat data/active_thesis.json

# TEST 6: Check injection
grep "previous_thesis injected" logs/trading_bot_2026-03-29.log

# TEST 8: Check Floki reasoning for thesis references
# (use DB query to get full debate text)

# TEST 11: Check for JSON errors
grep "Invalid.*JSON\|parse.*error\|not valid JSON" logs/trading_bot_2026-03-29.log

# TEST 14: Check FOLLOWUP
grep "FLOKI_FOLLOWUP" logs/trading_bot_2026-03-29.log

# TEST 15: Cost estimate
grep "FLOKI |.*cost=" logs/trading_bot_2026-03-29.log | tail -20
```
