# FLOKIWATCH — PROJECT STATE
## For Chat Continuation — March 6, 2026

---

## 1. What Is FlokiWatch

Automated XAU/USD (Gold) trading bot running on MT5 demo account. Named after the owner's dog Floki. Built with Python Brain (5-pillar analysis) + Claude AI Agent (decision authority) + EA Bridge (tick-by-tick MT5 execution). Owner (Hermano) manages the project with a developer ("meu amigo") and uses Claude as critical reviewer and strategic representative.

**GitHub:** `https://github.com/hhvivaldi/flokiwatch` (private repo, all changes must be committed)

---

## 2. Current Architecture (March 6, 2026)

```
5 Pillars + ML Ensemble (calculate scores every 5 min)
              ↓
    Central Brain (12-step analysis, scenario detection)
              ↓
    Claude AI Agent (Shadow Mode — observes, logs, doesn't execute)
              ↓
    EA Bridge (tick-by-tick execution on MT5) + Python fallback
              ↓
    Discord (11 channels) + Dashboard (FlokiWatch)
```

---

## 3. System Components — Current Status

| Component | Status | Details |
|-----------|--------|---------|
| 5-Pillar Brain | ✅ ACTIVE | Tech 30%, ML 25%, Mom 15%, News 20%, Cal 10% |
| ML Ensemble v3.1 | ✅ ACTIVE | 6 models (XGB+LGB+Cat × H1+H4), 48 features |
| EA Bridge | ✅ ACTIVE | USE_EA_BRIDGE = True, breakeven confirmed tick-by-tick |
| AI Agent (Claude) | ✅ SHADOW MODE | Observes BUY/SELL/HOLD_FORCED, logs decisions, doesn't execute |
| Discord | ✅ ACTIVE | 11 channels, multi-webhook routing with fallback |
| Dashboard | ✅ ACTIVE | FlokiWatch with Agent card, INTEL feed, history page |
| Breakeven | ✅ 50% of SL | Changed from 70% after backtest validation (+5.7% WR, +0.44 PF) |
| GPT Validator | ✅ ACTIVE | gpt-4o-mini, CONFIRM/BOOST/REDUCE ±15 |
| Volatility Guard | ✅ ACTIVE | 2-candle M5 logic, EXTREME/COOLING/NORMAL |

---

## 4. Key Configuration (config.py)

| Parameter | Value | Notes |
|-----------|-------|-------|
| BREAKEVEN_ATR_MULT | 0.50 | Changed from 0.70 on Feb 28 (backtest validated) |
| BRAIN_MIN_CONFIDENCE | 55.0% | Trades below this → HOLD FORCED |
| USE_EA_BRIDGE | True | EA handles execution + breakeven + trailing tick-by-tick |
| USE_AI_AGENT | True | Agent active in shadow mode |
| AI_AGENT_MODE | "shadow" | Shadow → Gate → Full Trader (phases) |
| AI_AGENT_MODEL | claude-sonnet-4-20250514 | Anthropic API |
| AI_AGENT_TIMEOUT | 30s | Fallback to Brain if timeout |
| Risk per trade | 2% | |
| Max daily loss | 6% | |
| SL range | 150-800 pips | ATR-based |
| Max positions | 3 | |

---

## 5. Live Performance (as of March 6, 2026)

**Overall (35 trades):** WR 48.57%, PF 0.99, P&L -$6.32

**Population B — Current system (28 trades):** WR 53.6%, PF 1.04

**Monthly:**
- Feb (26 trades): 12W/14L, WR 46.2%, PF 0.82, P&L -$50.29
- Mar (9 trades): 5W/4L, WR 55.6%, PF 1.30, P&L +$43.97

**Balance:** ~$989 (demo account)

**Key finding:** 50% BE change turned a -$50 day into +$85 day (Mar 2). Validated in live with 2 trades that would have been losses with old 70% BE.

---

## 6. AI Agent — Current State

**Mode:** Shadow (observes, logs, Brain executes)

**Data package (what Agent receives):**
- Raw candles: D1 (10), H4 (15), H1 (20), M5 (10)
- Indicators: RSI, MACD, EMAs, Bollinger, ATR, ADX
- Brain report: score, scenario, confidence, 5 pillar scores
- ML predictions: H1/H4 probabilities
- S/R Zones: 4-8 nearest with type, strength, touches
- Candlestick patterns: detected patterns with S/R multiplier
- MTF Trend: D1/H4 direction and alignment
- Volume Gate: volume ratio and status
- News & Macro: headlines, DXY, VIX, yields, calendar
- Open positions: tickets, P&L, duration, phase
- Session context: current session, today's W/L
- Agent Memory: last 5 own decisions with reasoning
- Delta Context: what changed since last call
- Portfolio: daily P&L, drawdown, risk budget remaining
- Regime: trending/ranging, ADX history, ATR comparison
- Trade Feedback: last 5 trades with Agent accuracy stats

**First 10 decisions (Mar 5-6):**
- 3 SIGNAL triggers: all REJECT (0 opens)
- 7 HOLD_FORCED triggers: all AGREE (WAIT/REJECT)
- Agent saved $48.77 by rejecting SELL #1516735232 (Asian session, RSI oversold)
- Agent missed +$25.50 by rejecting BUY #1516297137 (but had incomplete data at the time)
- Agent confidence always 15-25 — potentially too conservative

**Next phases:**
- Phase 2 (Gate Mode): Agent controls entries, Brain only generates signals
- Phase 3 (Full Trader): Agent controls entry + position management + session control

---

## 7. EA Bridge — Current State

**Status:** USE_EA_BRIDGE = True (production)

**Testing completed:** 6/7 steps passed
- ✅ Signal writing, EA signal reading, breakeven (202ms tick-by-tick), status reporting, Python reading, fallback
- ⏳ Trailing (Step 4) — not tested yet (price never reached trigger), same mechanism as breakeven

**Bugs fixed during testing:**
- Timestamp format (ISO → MQL5 dot format)
- Silent failure (EA now reports errors immediately)
- Timezone mismatch (Python uses MT5 server time)

**Phase 2 (Strategy Tester):** Not started. Will validate Python backtest with real tick data, variable spread, slippage.

---

## 8. Breakeven Analysis (Critical Finding)

**Original MFE analysis (19 trades):** 100% of losing trades went into profit first. Average MFE on losses: 163.7 pips. Breakeven at 70% of SL (~232 pips) was too far — trades reversed before reaching it.

**Backtest comparison (6 months, 225-247 trades):**
| Config | WR | PF | P&L |
|--------|-----|-----|-----|
| Baseline (70% SL) | 73.8% | 2.40 | +$2,323 |
| **50% SL (winner)** | **79.5%** | **2.84** | **+$2,355** |
| Fixed 100 pips | 79.4% | 1.80 | +$1,030 |
| Fixed 150 pips | 74.9% | 2.00 | +$1,480 |

Changed to 50% on Feb 28. Live validation: 2 trades activated BE that would NOT have with 70%.

---

## 9. Discord Channels (11)

**LIVE OPERATIONS:** xauusd-signals, dashboard-live, central-brain-decisions, executed-trades, bot-status-alerts
**PERFORMANCE:** daily-results, weekly-analysis, monthly-performance, backtests-and-optimization
**COMMANDS:** bot-requests (future), error-logs, updates-changelog

All automated except backtests-and-optimization and updates-changelog (manual).

---

## 10. Claude's Role (Rules)

Claude acts as **critical reviewer and strategic representative**, NOT a technical implementer.

**Mandatory rules:**
- Question inconsistencies, demand evidence for metrics
- Never write code
- Never accept narrative without proof (logs, screenshots, outputs)
- All changes must be committed to GitHub
- Any feature affecting bot decisions MUST be visible on dashboard
- Audit all numbers — cross-check with available data
- Structure: Part 1 (Analysis for Hermano) + Part 2 (Message for developer in English)

---

## 11. Pending Items (Priority Order)

| Priority | Item | Status |
|----------|------|--------|
| 1 | Breakeven dashboard visibility | Implemented, needs screenshot verification |
| 2 | Monitor Agent decisions (accumulate 20-30) | Ongoing |
| 3 | EA Bridge Step 4 (trailing test) | Waiting for trade to go deep enough |
| 4 | EA Bridge Phase 2 (Strategy Tester) | Not started |
| 5 | Agent Phase 2 (Gate Mode) | After 20+ shadow decisions analyzed |
| 6 | Agent prompt calibration (if too conservative) | Monitor first |
| 7 | GPT Validator display sign bug | Cosmetic, low priority |
| 8 | Startup log BE display fix | Cosmetic, low priority |

---

## 12. Key Decisions Made (Archive)

| Date | Decision | Evidence |
|------|----------|----------|
| Feb 24 | Tech Direction/Risk split ABANDONED | PF dropped from 2.24 to 1.71 in backtest |
| Feb 24 | Confidence threshold 55% confirmed optimal | 35%→$96 extra but PF degrades; 65%→$601 less P&L |
| Feb 28 | Breakeven changed 70% → 50% of SL | Backtest: +5.7% WR, +0.44 PF |
| Mar 2 | 50% BE validated in live | 2 trades protected that would have been losses |
| Mar 5 | EA Bridge activated for production | 6/7 test steps passed |
| Mar 5 | AI Agent deployed in Shadow Mode | First decision: excellent REJECT reasoning |
| Mar 5 | Agent triggers expanded to HOLD_FORCED | Agent evaluates blocked signals too |
| Mar 6 | 6 Agent data improvements deployed | Memory, Multi-TF, Feedback, Delta, Portfolio, Regime |

---

## 13. Known Issues

- Agent never opens trades (10/10 = REJECT/WAIT, confidence 15-25). May be prompt too cautious or all decisions during Asian session.
- EA Bridge trailing (Step 4) not validated in live yet.
- Backtest Python vs live performance gap: Backtest PF 2.84 vs Live PF 1.04. Strategy Tester validation needed.
- Dashboard breakeven visibility just implemented — needs screenshot verification.

---

## 14. Files Structure

```
XAUUSD/
├── main.py                    # Main orchestrator
├── config.py                  # All configuration
├── central_brain.py           # 12-step Brain
├── ai_agent.py                # Claude AI Agent
├── agent_prompts.py           # Agent system prompt v1.0
├── agent_data_builder.py      # Builds data package for Agent
├── ea_bridge.py               # Python-MT5 JSON bridge
├── monitor.py                 # Position management (BE, trailing)
├── executor.py                # MT5 order execution
├── technical_analyzer.py      # Pillar 1
├── ml_predictor.py            # Pillar 2
├── momentum_detector.py       # Pillar 3
├── news_score_hybrid.py       # Pillar 4
├── economic_calendar.py       # Pillar 5
├── gpt_confidence.py          # GPT Validator
├── volatility_guard.py        # Crash protection
├── safety_checks.py           # 10 safety checks
├── risk_manager.py            # Position sizing, SL/TP
├── alerts.py                  # Discord multi-webhook
├── state_writer.py            # Dashboard state JSON
├── db_writer.py               # SQLite history
├── cycle_memory.py            # Temporal memory (36 snapshots)
├── mql5/FlokiBridge.mq5       # MT5 EA for execution
├── dashboard/                 # FlokiWatch web UI
├── scripts/                   # Training, backtest, analysis
├── models/                    # ML model configs
├── data/                      # Runtime data
└── logs/                      # Daily log files
```

---

*Last updated: March 6, 2026 — End of session*
