# Atlas Blueprint — Technical Analyst Agent

> **Status:** PLANNING (not yet implemented)
> **Priority:** Phase 3, after Luna validated (30+ trades with Luna active)
> **Parent:** FLO-54 (Trading Office Expansion)

---

## 1. Role

Atlas is a pure **technical analyst**. He reads candles, indicators, support/resistance zones, Fibonacci levels, and candlestick patterns — then produces a structured technical brief for Floki.

Atlas does NOT recommend entries, exits, or trade directions. He describes the **technical setup** — what the chart says, where the key levels are, what patterns are forming, and whether timeframes are aligned. Floki decides what to do with the analysis.

**Analogy:** Luna tells Floki "the world is on fire" (macro). Atlas tells Floki "the chart says price is bouncing off the 61.8% Fibonacci with a bullish engulfing on rising volume" (technical).

---

## 2. Model Recommendation

**Primary candidate:** Kimi K2.5 (Moonshot AI)
- GPQA: 87.6% (strong reasoning)
- Cost: $0.60/M input, $2.50/M output
- Supports structured JSON output
- Available via OpenAI-compatible API

**Alternative:** MiMo-V2-Flash (Xiaomi)
- Already deployed for Luna + Echo
- Much cheaper ($0.10/$0.30 per M)
- May be sufficient for technical pattern recognition
- Avoids adding a third API provider

**Recommendation:** Start with MiMo-V2-Flash for cost efficiency. Upgrade to Kimi K2.5 only if MiMo quality is insufficient for complex chart reading (Fibonacci confluence, multi-timeframe alignment, pattern recognition at key levels).

---

## 3. Data Sources

Atlas reads from the **Scanner cache** — the same data Floki currently accesses via tools:

| Data | Source | Currently used by |
|------|--------|------------------|
| H1/M5/H4/D1 candles | `get_candles` tool → MT5 via Brain | Floki directly |
| RSI, MACD, EMAs, ATR, ADX, Bollinger | `get_indicators` tool → Brain cache | Floki directly |
| Support/Resistance zones | `get_sr_zones` tool → Brain cache | Floki directly |
| Fibonacci levels (H1/H4/D1) | `get_fibonacci_levels` tool → Brain cache | Floki directly |
| ML ensemble predictions | `get_ml_prediction` tool → Brain cache | Floki directly |
| MTF trend alignment | Brain cycle → data package | Floki via context |
| Volume gate | Brain cycle → data package | Floki via context |

**Key insight:** Atlas reads the SAME data Floki already has. The value is in the INTERPRETATION — Atlas can spend more tokens and reasoning on chart reading than Floki (who must also consider macro, news, positions, memory, Rex debate, and 20+ other tools).

---

## 4. Output Schema

**File:** `data/atlas_brief.json`

```json
{
  "timestamp": "ISO 8601",
  "setup_type": "BREAKOUT" | "PULLBACK" | "REVERSAL" | "CONTINUATION" | "RANGE" | "NO_SETUP",
  "setup_quality": 1-10,
  "direction": "BULLISH" | "BEARISH" | "NEUTRAL",
  "confidence": 1-10,

  "entry_zone": {
    "price_low": 5180.0,
    "price_high": 5195.0,
    "rationale": "Fib 61.8% confluence with H4 EMA50"
  },
  "sl_level": {
    "price": 5155.0,
    "rationale": "Below swing low + 20 pip buffer"
  },
  "tp_level": {
    "price": 5250.0,
    "rationale": "Previous H4 high + resistance zone"
  },
  "invalidation": "Close below 5150 on H1 invalidates setup",

  "key_levels": [
    {"price": 5200.0, "type": "resistance", "source": "Fib 50%"},
    {"price": 5175.0, "type": "support", "source": "H4 EMA50"},
    {"price": 5150.0, "type": "support", "source": "swing_low"}
  ],

  "pattern_detected": {
    "name": "bullish_engulfing",
    "timeframe": "H1",
    "location": "at_support",
    "quality": "high"
  },

  "timeframe_alignment": {
    "D1": "BULLISH",
    "H4": "BULLISH",
    "H1": "NEUTRAL",
    "M5": "BEARISH",
    "aligned": false,
    "note": "H1/M5 not yet confirming higher timeframes"
  },

  "indicators_summary": {
    "rsi": {"value": 45.2, "interpretation": "neutral, room to run"},
    "macd": {"histogram": "rising", "interpretation": "momentum building"},
    "adx": {"value": 28.5, "interpretation": "trend confirmed"},
    "volume": {"ratio": 1.3, "interpretation": "above average"}
  },

  "summary": "Bullish engulfing at Fib 61.8% support with D1/H4 alignment. ADX confirms trend. Waiting for H1 close above 5190 for confirmation.",

  "data_snapshot": {
    "price": 5188.50,
    "ema200": 5120.30,
    "atr": 35.2
  }
}
```

---

## 5. Floki Integration — THE KEY DECISION

### Option A: Atlas REPLACES Floki's direct technical tools

Like Luna replaced `get_macro`/`get_headlines`:
- REMOVE from Floki: `get_indicators`, `get_sr_zones`, `get_fibonacci_levels`, `get_candles`
- ADD: `get_atlas_brief` (reads atlas_brief.json)
- Floki keeps: `get_current_price`, `get_ml_prediction`, `get_open_positions`, all trading tools

**Pros:**
- Floki's tool budget drops from ~25 to ~15 tools per cycle
- Atlas spends dedicated reasoning on chart reading
- Gemini tokens saved (no raw indicator data in context)
- Cleaner separation of concerns

**Cons:**
- **HIGH RISK** — If Atlas misinterprets a chart pattern, Floki has NO way to verify
- Floki loses ability to "read the chart himself" — must trust Atlas blindly
- Atlas runs every 5-15 min; Floki may need real-time price data between briefs
- Debugging is harder — was it Atlas's analysis or Floki's interpretation that caused a bad trade?

### Option B: Atlas SUPPLEMENTS Floki's tools (Floki keeps both)

- KEEP all of Floki's current technical tools
- ADD: `get_atlas_brief` as additional context
- Floki can compare Atlas's interpretation with raw data

**Pros:**
- Zero risk — Floki can always verify Atlas's analysis
- Gradual trust building (shadow mode → supplementary → primary)
- If Atlas fails, Floki still has full technical access

**Cons:**
- More tools = more Gemini tokens per cycle
- Atlas adds cost without reducing Floki's workload
- Unclear value proposition — why pay for Atlas if Floki reads the same data?

### RECOMMENDATION: Option B first, then graduated migration

1. **Shadow mode (2 weeks):** Atlas runs alongside, output logged but Floki doesn't read it. Compare Atlas setups vs Floki's actual decisions.
2. **Supplementary mode (2 weeks):** Floki gets `get_atlas_brief` as additional tool. Both Atlas brief and raw technical tools available. Track: does Floki use Atlas brief? Does it improve decisions?
3. **Primary mode (if validated):** Remove `get_indicators`, `get_sr_zones`, `get_fibonacci_levels`. Keep `get_candles` (Floki still needs to "see" price) and `get_current_price`.

**Critical difference from Luna:** Luna replaced data Floki CANNOT interpret well (DXY/VIX correlation patterns, forced liquidation detection). Atlas would replace data Floki CAN read himself. The bar for Atlas to earn Floki's trust is much higher.

---

## 6. Trade Room

### Atlas Card
- Avatar: Letter "A" placeholder (like Luna has "L")
- Color: `#00BFA5` (teal green — technical analysis)
- Name: ATLAS
- Role: TECHNICAL ANALYST
- Status pill: setup type badge (BREAKOUT/PULLBACK/REVERSAL/etc.)
- Metrics: setup quality X/10, direction arrow, confidence
- Expand panel: key levels, pattern detected, timeframe alignment, summary

### Chat Messages
- Structured like Luna: setup badge + direction + key levels + summary
- BREAKOUT/REVERSAL setups: highlighted border
- NO_SETUP: muted styling

### Filter Tab
- Add TECHNICAL tab to feed filter bar (between MACRO and ALERTS)
- Shows only Atlas messages (author="ATLAS")

---

## 7. Frequency

### Option A: Scheduled (every 5 min)
- Pro: Always fresh brief for Floki
- Con: Higher cost, most cycles have no new setup

### Option B: On-demand (Floki requests)
- Pro: Only runs when Floki needs it
- Con: Adds 3-5s latency to Floki's cycle, complex to implement

### Option C: Event-driven (Simba triggers)
- Atlas runs when: price touches key S/R zone, pattern forms, volume spike
- Pro: Only runs when chart structure changes
- Con: Need Simba integration for Atlas triggers

### RECOMMENDATION: Option A (scheduled every 5 min) with smart caching
- If price moved < 0.1% and no new H1 candle closed → reuse last brief
- Estimated 20-30 real analyses per day (vs 288 if every 5 min)
- Cost: ~$0.50-1.00/day with MiMo, ~$3-5/day with Kimi K2.5

---

## 8. Risk Assessment

**Why Atlas is higher risk than Luna:**

| Factor | Luna (macro) | Atlas (technical) |
|--------|-------------|-------------------|
| Data Floki can verify | NO — Floki can't interpret DXY/VIX correlations | YES — Floki reads the same chart |
| Impact of wrong analysis | Floki might overtrade in DANGER or undertrade in SAFE | Floki might enter at wrong level, miss pattern, or ignore reversal signal |
| Fallback quality | Deterministic rules are decent for macro | No good fallback — raw indicators without interpretation lose value |
| Blast radius | Macro bias affects trade frequency | Technical analysis affects EVERY trade entry/exit level |
| Debugging | Easy — compare Luna env vs actual VIX/DXY | Hard — was Atlas wrong or did price just not respect the level? |

**Mitigation:**
- Extended shadow mode (minimum 2 weeks, 30+ trades)
- A/B comparison: Atlas setup quality vs Floki's actual trade outcomes
- Never remove `get_candles` — Floki must always see price
- Kill switch: `ATLAS_ENABLED = False` instantly restores all technical tools

---

## 9. Validation Plan

### Phase 1: Shadow Mode (2 weeks)
- Atlas runs every 5 min, writes atlas_brief.json
- Floki does NOT read it (no `get_atlas_brief` tool)
- Post-hoc comparison: did Atlas identify the setup Floki traded?
- Metrics: setup accuracy, direction accuracy, level precision (entry ±20 pips)

### Phase 2: Supplementary Mode (2 weeks)
- `get_atlas_brief` added to Floki's tool list
- Floki keeps ALL technical tools
- Track: tool_trace shows which tools Floki uses
- If Floki consistently ignores Atlas → Atlas is not adding value
- If Floki uses Atlas AND outcomes improve → proceed to Phase 3

### Phase 3: Primary Mode (graduated)
- Remove `get_indicators` first (lowest risk — Atlas summary covers this)
- Wait 1 week, evaluate
- Remove `get_sr_zones` + `get_fibonacci_levels` (medium risk)
- Wait 1 week, evaluate
- KEEP `get_candles` permanently — Floki always reads the chart

### Kill Criteria
- Win rate drops >5% after any phase transition → revert
- 3 consecutive trades where Atlas brief was clearly wrong → revert
- Atlas API availability <95% → revert to Option B

---

## 10. Dependencies

Must be stable before Atlas implementation:

| Dependency | Status | Why |
|-----------|--------|-----|
| Luna validated (30+ trades) | PENDING — goes live Monday | Proves the analyst-agent pattern works |
| Simba conditions stable | DONE (FLO-49/52) | Atlas may use Simba for event triggers |
| Feed health monitoring | DONE (FLO-42) | Pattern for monitoring Atlas API health |
| Trade Room agent cards | DONE (FLO-58) | Card template ready (copy Luna pattern) |
| MiMo API stable for Echo+Luna | PENDING — first week of production | Proves Xiaomi API reliability at scale |
| Floki prompt v1.6 stable | PENDING — Monday deployment | Luna integration must not degrade Floki |

**Estimated start:** 2-3 weeks after Luna goes live (earliest April 7, 2026)

---

## Summary

Atlas is a valuable but risky addition. Unlike Luna (which gave Floki data he couldn't interpret), Atlas gives Floki a second opinion on data he already reads. The value is in dedicated reasoning time and pattern detection, but the risk is Floki losing his ability to verify technical analysis independently.

**Approach:** Shadow mode → Supplementary → Graduated primary. Never rush. Never remove `get_candles`.
