# FlokiWatch — XAU/USD Trading Bot

Autonomous multi-agent trading system for XAU/USD (Gold) on MetaTrader 5. Six specialized AI agents collaborate through the **Trading Office** architecture.

## Overview

The bot operates **100% autonomously** on MetaTrader 5:
- AI agents analyze markets and decide trades (no manual intervention)
- Position management via EA Bridge (tick-by-tick breakeven, trailing stop)
- 25 RSS news feeds monitored 24/7 for breaking events
- Real-time Trade Room dashboard + Discord alerts
- Daily performance audit by independent Sage auditor

**Current state:** DEMO mode, ICMarkets. Balance: $1064.02, 81 trades (Population B).

## Architecture — Trading Office

```
                        ┌──────────────────────────┐
                        │      TRADING OFFICE       │
                        ├──────────────────────────┤
                        │                          │
  ┌─────────┐  ┌────────┴────────┐  ┌───────────┐ │
  │  FLOKI  │  │    BRAIN        │  │   ECHO    │ │
  │ Agent   │  │  Data Pipeline  │  │  News     │ │
  │ Gemini  │  │  (Python, 60s)  │  │  Sentinel │ │
  │ 3 Flash │  │  Tech/ML/News/  │  │  GPT-4o-  │ │
  │         │  │  Calendar/S&R   │  │  mini     │ │
  │ DECIDES │  │  NO DECISIONS   │  │  24/7 RSS │ │
  └────┬────┘  └────────┬────────┘  └─────┬─────┘ │
       │                │                  │       │
       │  ┌─────────┐   │   ┌──────────┐   │       │
       ├──│   REX   │   │   │  SIMBA   │   │       │
       │  │ Debate  │   │   │ Watchdog │   │       │
       │  │ GPT-4o  │   │   │ (Python) │───┘       │
       │  └─────────┘   │   └──────────┘           │
       │                │                          │
       │  ┌─────────┐   │                          │
       │  │  SAGE   │   │                          │
       │  │ Auditor │   │                          │
       │  │ Daily   │   │                          │
       │  └─────────┘   │                          │
       ▼                ▼                          │
  ┌─────────────────────────────┐                  │
  │  EXECUTOR + EA BRIDGE       │                  │
  │  MT5 Orders (tick-by-tick)  │                  │
  └─────────────────────────────┘                  │
                        │                          │
                        └──────────────────────────┘
```

### Agent Roles

| Agent | Model | Role | Cadence |
|-------|-------|------|---------|
| **Floki** | Gemini 3 Flash | Portfolio manager — sole trading decisor (WAIT/OPEN/CLOSE/ADJUST) | 5-30 min (self-scheduled via `set_next_check`) |
| **Rex** | GPT-4o | Debate partner — challenges Floki's reasoning (AGREE/DISAGREE) | On each Floki decision |
| **Simba** | Python (no AI cost) | Watchdog — monitors wake/watch conditions, wakes Floki | Every 30s |
| **Sage** | Gemini | Performance auditor — daily trade review + recommendations | Daily at 21:00 UTC |
| **Echo** | GPT-4o-mini | News sentinel — 25 RSS feeds, classifies CRITICAL/IMPORTANT/ROUTINE | Every 5 min |
| **Luna** | TBD | Macro analyst (planned Phase 3) | — |
| **Atlas** | TBD | Technical analyst (planned Phase 3) | — |

### Data Pipeline (Brain)

The Brain runs every 60 seconds and feeds raw data to agents:
- **Technical**: RSI, MACD, Bollinger, EMAs, ATR, S/R zones, Fibonacci
- **ML Ensemble**: 6 models (XGBoost + LightGBM + CatBoost × H1 + H4)
- **News**: 25 RSS feeds (14 Google News + 11 direct) + DXY/VIX/Yields
- **Calendar**: Economic events from MQL5/FCS API
- **Momentum**: ADX, volume, breakout detection

The Brain does NOT make trading decisions — Floki is the sole decisor.

### Scheduling & Triggers

| Trigger | Who Fires | What Happens |
|---------|-----------|-------------|
| `SCHEDULED` | Timer (Floki's `set_next_check`) | Floki analyzes full market snapshot |
| `SIMBA_WAKE` | Simba detects wake condition | Floki called immediately |
| `SIMBA_WATCH` | Simba detects watch condition | Floki reviews open position |
| `ECHO_CRITICAL` | Echo classifies breaking news | Floki called immediately (max 2/hr) |

Legacy triggers (entry conditions, breakout, session change) are **disabled** — the allowlist gate in `agent_proactive_out_of_cycle()` only passes SCHEDULED, SIMBA_WAKE, SIMBA_WATCH, ECHO_CRITICAL.

## Project Structure

```
flokiwatch/
├── main.py                 # Orchestrator — main loop, agent scheduling
├── config.py               # Configuration (loads from .env)
├── ai_agent.py             # Floki agent (Gemini 3 Flash, tool-use)
├── agent_tools.py          # Floki's 20+ tools (market data, trading, memory)
├── agent_prompts.py        # System prompt builder
├── rex_validator.py        # Rex debate partner (GPT-4o)
├── echo_sentinel.py        # Echo news sentinel (GPT-4o-mini, RSS feeds)
├── simba_watcher.py        # Simba watchdog (Python, zero AI cost)
├── sage_auditor.py         # Sage daily auditor
├── central_brain.py        # Data pipeline (5 pillars, no decisions)
├── monitor.py              # Position monitoring (breakeven, trailing)
├── executor.py             # MT5 order execution
├── risk_manager.py         # Position sizing, SL/TP calculation
├── safety_checks.py        # Safety validations + market hours
├── ml_predictor.py         # ML ensemble predictions
├── news_score_hybrid.py    # News scoring + 25 RSS feeds
├── db_writer.py            # SQLite history database
├── state_writer.py         # Dashboard state file (bot_state.json)
├── dashboard/
│   ├── server.py           # FastAPI backend (port 8080)
│   └── static/
│       ├── index.html      # Main dashboard
│       ├── trade_room.html # Trade Room (agent cards + live feed)
│       ├── history.html    # Trade history + equity curve
│       ├── app.js          # Dashboard frontend logic
│       └── history.js      # History page logic
├── data/                   # Runtime data (bot_state.json, alerts, etc.)
├── models/                 # ML model configs (JSON)
├── logs/                   # Log files
└── mql5/                   # EA Bridge (FlokiBridge.mq5)
```

## Setup

### 1. Clone and Install

```bash
git clone https://github.com/hhvivaldi/flokiwatch.git
cd flokiwatch
pip install -r requirements.txt
```

### 2. Configure Environment

Copy `.env.example` to `.env` and fill in credentials:

```bash
cp .env.example .env
```

Required variables:
- `MT5_ACCOUNT`, `MT5_PASSWORD`, `MT5_SERVER`, `MT5_TERMINAL_PATH`
- `DISCORD_WEBHOOK_URL` (+ channel-specific webhooks)
- `OPENAI_API_KEY` — For Rex (GPT-4o) and Echo (GPT-4o-mini)
- `GEMINI_API_KEY` — For Floki (Gemini 3 Flash)
- `ECHO_API_KEY` — Echo dedicated key (falls back to OPENAI_API_KEY)

### 3. Start the Bot

```bash
python main.py
```

### 4. Start the Dashboard

```bash
python -m uvicorn dashboard.server:app --host 0.0.0.0 --port 8080
```

Access at `http://localhost:8080`

## Dashboard

- **Main Dashboard** (`/`): Balance, P&L, positions, Intel Feed with Echo badges
- **Trade Room** (`/trade-room`): 6 agent cards, live feed with structured messages, NEWS filter
- **History** (`/history`): Equity curve (anchored to real balance), stat cards (Population B), monthly breakdown
- **About** (`/about`): System info and agent descriptions

### Trade Room Features

- Agent cards with animated avatars (GIF rotation every 60s)
- Market hours personality: REST DAY (weekend) / COFFEE BREAK (daily pause) / ON WATCH (Echo)
- Resting cards dimmed (opacity 0.45, desaturated) — Echo stays bright 24/7
- Structured message rendering (Floki decisions, Rex debates, Simba patrols, Echo alerts)
- Feed filter tabs: ALL / DECISIONS / CONFLICTS / ANALYSIS / NEWS / ALERTS

## Performance (Population B)

Stats filtered to ticket >= 8, open_time >= 2026-02-16:

| Metric | Value |
|--------|-------|
| Trades | 81 |
| Win Rate | 56.79% |
| Profit Factor | 0.95 |
| Total P&L | +$64.02 |
| Balance | $1,064.02 |
| Max Drawdown | $242.43 |

Equity curve anchored to real balance from `bot_state.json` (accounts for swap/commission not in DB profit column).

## Safety Features

- Max 1 position at a time
- ATR-based SL/TP (1.5× ATR SL, 3.0× ATR TP)
- 3 consecutive losses → 24h pause
- Daily loss > 6% → block
- Extreme volatility guard (>1.8% M5 candle)
- High-impact news release → block
- Spread > 5 pips → block
- Echo: max 2 CRITICAL wakes per hour
- Echo: daily cost cap ($1.00/day)

## Disclaimer

1. **Always test in DEMO mode first**
2. **Start with small capital** in LIVE mode
3. **Monitor via Discord and Trade Room** during initial weeks
4. **Keep MT5 running** on VPS or dedicated machine

---

**Trading involves risk of capital loss. Use at your own risk.**
