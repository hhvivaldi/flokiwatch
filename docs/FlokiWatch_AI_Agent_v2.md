# FLOKIWATCH AI AGENT
## THE TRADER — NOT THE VALIDATOR
### Design Document v2 — March 2026

---

## 1. Core Concept

**The Agent is the trader.** It is not a validator that approves or rejects the Brain's decisions. It is an autonomous AI trader that receives raw market data, analysis reports, news, and position information, then reasons like an expert trader and makes its own decisions.

The existing 5-pillar Brain becomes an **analyst** that prepares a report. The ML ensemble is a **quantitative advisor**. The Agent is the **portfolio manager** who reads all reports, looks at the charts, checks the news, and decides.

The Agent can agree with the Brain, disagree with it, or see opportunities the Brain missed entirely. It has full authority over trade entry, exit, and timing — within hard risk limits it cannot override.

---

## 2. How to Think About It

Imagine a trading desk. The Brain is the junior analyst who runs the numbers every 5 minutes and writes a report: "Technical score 67, momentum strong, ML slightly bullish, news neutral." The ML ensemble is the quant who adds: "My models say 65% probability of upward movement."

The Agent is the senior trader who reads both reports, then looks at the actual chart and says: "I see your numbers, but look at the last 5 candles — there's distribution happening at this level. The volume is drying up. And the DXY just reversed. I'm not buying here, even though your score says 67. Wait for a pullback."

That contextual reasoning — connecting patterns, reading sequences, understanding market behavior — is what makes the Agent fundamentally different from the rule-based Brain.

---

## 3. Architecture

### 3.1 Current System

```
Market Data → 5 Pillars → Central Brain (rules) → Decision → MT5
```

The Brain is both analyst AND decision-maker. Fixed thresholds. No contextual reasoning.

### 3.2 New System

```
Market Data → 5 Pillars + ML (calculate everything)
                    ↓
    Claude AI Agent (receives ALL data, reasons, decides)
                    ↓
    EA Bridge (tick-by-tick execution + protection)
```

The Brain becomes the analyst. Claude is the decision-maker. The EA Bridge is the executor.

---

## 4. What the Agent Receives Every 5 Minutes

The Agent receives a structured data package with everything a human trader would want on their screen:

| Category | Data | Purpose |
|----------|------|---------|
| Raw Price Data | Last 20 H1 candles (OHLCV), last 10 M5 candles, current bid/ask | Read the chart as sequence, not snapshot |
| Indicators | RSI, MACD, EMAs (9/21/50), Bollinger, ATR, ADX — current + trend | Quantitative context (numbers, not interpretation) |
| Brain Report | Score, scenario, confidence, 5 pillar scores, confirmations, alerts | Analyst's opinion — reference, not authority |
| ML Predictions | H1/H4 probabilities, model confidence, ensemble agreement | Quant advisor's forecast |
| News & Macro | Top headlines, DXY, VIX, 10Y yields, calendar events | Fundamental and geopolitical context |
| Open Positions | Tickets, direction, entry, P&L, duration, SL, TP, phase, MFE | Current exposure and portfolio state |
| Session Context | Current session, today's W/L/P&L, last 5 trade results | Performance awareness and session behavior |
| Volatility | Guard status (NORMAL/EXTREME/COOLING), M5 movement | Risk environment assessment |

---

## 5. Agent Decision Powers

### 5.1 Trade Entry

- **OPEN_BUY / OPEN_SELL** — Open a new position. Agent specifies confidence and reasoning. SL/TP calculated by risk manager (Agent cannot override risk parameters).
- **REJECT_SIGNAL** — Brain suggested a trade, but Agent disagrees. Reason logged.
- **WAIT** — Interesting setup but timing is wrong. Re-evaluate next cycle.

### 5.2 Position Management

- **CLOSE_POSITION** — Close a specific open position NOW. Agent sees something the trailing stop can't (e.g., reversal pattern forming, volume drying up).
- **TIGHTEN_SL** — Move stop loss tighter than normal trailing would. Agent sees increasing risk.
- **HOLD_POSITION** — Keep position, let EA Bridge manage trailing normally.

### 5.3 Session Control

- **PAUSE_TRADING** — Stop opening new trades. Duration and reason specified. (e.g., "Pause 2 hours — 3 consecutive losses in Asian session")
- **RESUME_TRADING** — Lift a previous pause.

---

## 6. Agent System Prompt (Core Personality)

The system prompt defines WHO the Agent is. This is critical for consistent behavior. Key elements:

- **Identity:** "You are an expert XAU/USD trader with 15 years of experience. You trade Gold exclusively and understand its safe-haven dynamics, correlation with DXY and yields, and response to geopolitical events."
- **Philosophy:** "Capital preservation first. You would rather miss 10 opportunities than take 1 bad trade. You are patient, disciplined, and never trade out of boredom or FOMO."
- **Method:** "You read price action as a sequence, not isolated candles. You look for context: where is price relative to recent structure? Is volume confirming the move? Are higher timeframes aligned?"
- **Risk Rules:** "You CANNOT override: max 2% risk per trade, max 3 positions, max 6% daily loss, SL range 150-800 pips. These are non-negotiable."
- **Output:** "Always respond with structured JSON containing: decision, reasoning (2-3 sentences), confidence (0-100), and any position management actions."

**The system prompt will be ~1,500 tokens.** It stays constant across all calls. The variable data (market, positions, etc.) is ~5,000-10,000 tokens per call.

---

## 7. When the Agent Is Called

| Trigger | What Happens | Cost Impact |
|---------|-------------|-------------|
| Brain says BUY or SELL | Agent receives full context + Brain signal. Decides OPEN or REJECT. | ~5-15 calls/day. Low cost. |
| Open position exists | Agent reviews position every cycle. Can CLOSE, TIGHTEN, or HOLD. | ~50-200 calls/day when in trade. Medium cost. |
| Significant event | VIX spike >15%, extreme volatility, major news headline. Agent assesses impact. | Rare. ~1-5 calls/day. |
| Brain says HOLD (no position) | Agent is NOT called. No cost. Brain HOLD with no open position = no action needed. | Zero cost. This is 80%+ of cycles. |

**Key optimization:** The Agent is NOT called every 5 minutes. It is only called when there is a decision to make (signal to evaluate, position to manage, or event to assess). During quiet HOLD periods with no positions, the Agent sleeps. This keeps costs at $30-80/month instead of $150+.

---

## 8. Safety and Fallback

- **Timeout:** If the Agent doesn't respond within 30 seconds, the Brain's decision is used (current behavior).
- **API failure:** If Anthropic API is down, system continues with Brain decisions. Agent is an enhancement, not a dependency.
- **Risk limits:** Agent CANNOT override risk parameters. Max lot, max positions, max daily loss, SL range — all enforced in code before the Agent's decision reaches MT5.
- **Kill switch:** USE_AI_AGENT = True/False in config.py. Disable instantly if needed. System reverts to Brain-only mode.
- **Shadow mode:** Agent decides but Brain executes. Both decisions logged for comparison. Zero risk, full learning.

---

## 9. Implementation Phases

### Phase 1: Shadow Mode (2 weeks)

Agent runs alongside the Brain. Both make decisions. Only the Brain's decision executes. Agent's decision is logged to Discord (#central-brain-decisions) for comparison.

**Goal:** Validate that the Agent makes better decisions than the Brain on historical patterns. If Agent would have blocked the 2 losing trades on Feb 24 (bought during 115-pip decline), that's proof of value.

### Phase 2: Gate Mode (Agent decides entry)

Agent controls trade entry. Brain generates signals, Agent approves/rejects. Position management still handled by EA Bridge (trailing, breakeven).

**Goal:** Improve live win rate by filtering bad entries. Target: WR improvement of +5-10% vs Brain-only.

### Phase 3: Full Trader Mode

Agent controls entry AND position management. Can close trades, tighten SL, pause trading. EA Bridge still runs as mechanical safety net (breakeven, max drawdown).

**Goal:** Agent actively manages trades based on market reading, not just mechanical rules.

---

## 10. Cost Estimate

| Phase | Calls/Day | Tokens/Call | Monthly Cost |
|-------|-----------|-------------|-------------|
| Shadow Mode | 5-15 | ~8K | $15-30 |
| Gate Mode | 5-15 | ~8K | $15-30 |
| Full Trader (with positions) | 20-100 | ~10K | $50-80 |

**Model recommendation:** Claude Sonnet 4 for cost efficiency. Claude Opus 4 if reasoning quality needs improvement. Start with Sonnet, upgrade if needed.

---

## 11. What Stays the Same

- 5-pillar analysis system (calculates scores every 5 minutes)
- ML Ensemble v3.1 (6 models, 48 features)
- EA Bridge for tick-by-tick execution (breakeven at 50% SL, trailing)
- Discord multi-channel alerts (11 channels)
- FlokiWatch dashboard
- Risk management rules (2% per trade, ATR-based SL/TP)
- Safety checks (max positions, daily loss, spread)
- SQLite trade history

---

## 12. What the Owner Provides

- **Anthropic API key:** Create account at console.anthropic.com, add credits ($20-50 to start), generate API key.
- **Environment variable:** Add ANTHROPIC_API_KEY to .env file.
- **Model choice:** Claude Sonnet 4 recommended to start (best cost/quality balance).

---

## 13. Dashboard Visibility

The Agent's decisions MUST be visible on the dashboard. New elements required:

- **Agent Decision Card:** Shows APPROVE/REJECT/CLOSE/WAIT with reasoning summary.
- **Agent vs Brain comparison:** When Agent disagrees with Brain, show both decisions side by side.
- **Agent activity log:** Last 10 Agent decisions with timestamps and outcomes.
- **Discord integration:** #central-brain-decisions channel shows Agent reasoning for every decision.

---

*FlokiWatch AI Agent — Design Document v2 — Internal — March 2026*
