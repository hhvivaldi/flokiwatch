# FlokiWatch — XAU/USD Trading Bot

Autonomous multi-agent trading system for XAU/USD (Gold) on MetaTrader 5. Seven specialized AI agents collaborate through the **Trading Office** architecture.

## Overview

The bot operates **100% autonomously** on MetaTrader 5:
- AI agents analyze markets and decide trades (no manual intervention)
- Floki manages positions via adjust_trade (no EA autonomous trailing)
- 20 MT5 instruments + Yahoo/FRED data for cross-market context
- 25 RSS news feeds monitored 24/7 for breaking events
- Real-time Trade Room dashboard with 5-section macro panel
- Daily performance audit by independent Sage auditor
- Active thesis persistence for inter-cycle continuity

**Current state:** DEMO mode, Capital Point. Balance: $813.76, 101 trades (Population B).

## Architecture — Trading Office

```
                        ┌──────────────────────────┐
                        │      TRADING OFFICE       │
                        ├──────────────────────────┤
                        │                          │
  ┌─────────┐  ┌────────┴────────┐  ┌───────────┐ │
  │  FLOKI  │  │    BRAIN        │  │   ECHO    │ │
  │ Agent   │  │  Data Pipeline  │  │  News     │ │
  │ GPT-5.4 │  │  (Python, 60s)  │  │  Sentinel │ │
  │ 30 tools│  │  Tech/ML/News/  │  │  MiMo-V2  │ │
  │         │  │  Calendar/S&R   │  │  Flash    │ │
  │ DECIDES │  │  NO DECISIONS   │  │  24/7 RSS │ │
  └────┬────┘  └────────┬────────┘  └─────┬─────┘ │
       │                │                  │       │
       │  ┌─────────┐   │   ┌──────────┐   │       │
       ├──│   REX   │   │   │  SIMBA   │───┘       │
       │  │ Co-pilot│   │   │ Watchdog │           │
       │  │GPT-5mini│   │   │ (Python) │           │
       │  │ 9 tools │   │   └──────────┘           │
       │  └─────────┘   │                          │
       │                │                          │
       │  ┌─────────┐   │   ┌──────────┐           │
       │  │  SAGE   │   │   │  LUNA    │           │
       │  │ Auditor │   │   │  Macro   │           │
       │  │ Daily   │   │   │  MiMo-V2 │           │
       │  └─────────┘   │   │ MT5+Yahoo│           │
       ▼                ▼   └──────────┘           │
  ┌─────────────────────────────┐                  │
  │  EXECUTOR + EA BRIDGE       │                  │
  │  MT5 Orders (pure executor) │                  │
  └─────────────────────────────┘                  │
                        │                          │
                        └──────────────────────────┘
```

### Agent Roles

| Agent | Model | Role | Tools | Cadence |
|-------|-------|------|-------|---------|
| **Floki** | GPT-5.4 | Portfolio manager — sole trading decisor (WAIT/OPEN/CLOSE/ADJUST) | 28 | 5-30 min (self-scheduled) |
| **Rex** | GPT-4o | Analyst — provides insights + Bull/Bear structured debate (FLO-190). 6 standard + 5 unique tools. | 11 | Bull/Bear every cycle + insights on demand |
| **Research Mgr** | Gemini 3 Flash | Picks winner between Rex Bull and Rex Bear. Produces verdict with trigger levels (FLO-194). | — | Every Floki cycle (after debate) |
| **Simba** | Python (no AI cost) | Watchdog — monitors conditions, wakes Floki | — | Every 30s |
| **Sage** | Gemini | Performance auditor — daily trade review + recommendations | — | Daily at 21:00 UTC |
| **Echo** | MiMo-V2-Flash | News sentinel — 25 RSS feeds, classifies CRITICAL/IMPORTANT/ROUTINE | — | Every 5 min |
| **Luna** | MiMo-V2-Flash | Macro analyst — MT5 enriched (20 instruments) + Yahoo/FRED | — | Every 15 min |

### Key Features

- **Delta-based continuity**: Each cycle shows objective numeric deltas since the last cycle (price, RSI, ADX, MACD, regime). No thesis anchoring.
- **Rex Bull/Bear debate (FLO-190/194)**: Before each Floki cycle, Rex Bull argues gold goes UP (BUY) and Rex Bear argues gold goes DOWN (SELL), both in parallel. Research Manager picks the winner and produces a verdict for Floki.
- **Cross-market context**: 15 MT5 instruments (metals, forex, indices, energy, crypto, futures) + Yahoo/FRED data.
- **Rex market intelligence**: 11 tools (5 unique: session performance, divergence scan, correlation check, regime history, reflexion search). Provides insights, not approval.
- **FOLLOWUP mechanism**: If Floki decides OPEN/CLOSE/ADJUST but forgets to call the tool, system injects a reminder turn.
- **Position management**: Floki is sole manager — EA is pure executor with 9999-pip BE/trailing (never triggers).

### Data Pipeline (Brain)

The Brain runs every 60 seconds and feeds raw data to agents:
- **Technical**: RSI, MACD, Bollinger, EMAs (50+200), ATR, S/R zones, Fibonacci
- **ML Ensemble**: 6 models (XGBoost + LightGBM + CatBoost × H1 + H4)
- **Cross-market**: 15 MT5 instruments via `market_context_fetcher.py`
- **News**: 25 RSS feeds (14 Google News + 11 direct) + DXY/VIX/Yields
- **Calendar**: Economic events from MQL5/FCS API
- **Momentum**: ADX, volume, breakout detection

## Project Structure

```
flokiwatch/
├── main.py                 # Orchestrator — main loop, agent scheduling, thesis persistence
├── config.py               # Configuration (loads from .env)
├── ai_agent.py             # Floki agent (GPT-5.4, OpenAI tool-use, 30 tools)
├── agent_tools.py          # Floki's 30 tools (market data, trading, memory)
├── agent_prompts.py        # System prompt (91 lines, ~1,314 tokens, 9 sections)
├── rex_validator.py        # Rex analyst (GPT-5 mini, 11 tools, insights not AGREE/DISAGREE)
├── market_context_fetcher.py # MT5 correlated instruments (15 symbols, 60s cache)
├── echo_sentinel.py        # Echo news sentinel (MiMo-V2-Flash, RSS feeds)
├── luna_analyst.py          # Luna macro analyst (MiMo-V2-Flash, MT5+Yahoo+FRED)
├── simba_watcher.py         # Simba watchdog (Python, zero AI cost)
├── sage_auditor.py          # Sage daily auditor (Gemini)
├── central_brain.py         # Data pipeline (5 pillars, no decisions)
├── monitor.py               # Position monitoring
├── executor.py              # MT5 order execution + EA Bridge
├── floki_position_manager.py # EA params (9999 triggers = pure executor)
├── risk_manager.py          # Position sizing, SL/TP calculation
├── safety_checks.py         # Market hours + opposing position guard (FLO-85)
├── trade_reflexion.py       # Post-trade reflexion engine (GPT-5.4, FLO-137)
├── db_writer.py             # SQLite history database
├── state_writer.py          # Dashboard state (bot_state.json + market_context)
├── dashboard/
│   ├── server.py            # FastAPI backend (port 8080)
│   └── static/
│       ├── index.html       # Main dashboard
│       ├── trade_room.html  # Trade Room (5-section macro panel)
│       ├── app.js           # Dashboard frontend (5-section macro render)
│       └── style.css        # FlokiWatch theme
├── data/                    # Runtime data (bot_state.json, active_thesis.json, etc.)
├── logs/                    # Daily log files (auto-rotated at midnight)
└── mql5/                    # EA Bridge (FlokiBridge.mq5)
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
- `OPENAI_API_KEY` — For Floki (GPT-5.4) and Rex (GPT-5 mini)
- `LUNA_API_KEY` — For Luna + Echo (MiMo-V2-Flash via Xiaomi API)
- `GEMINI_API_KEY` — For Sage auditor

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

- **Main Dashboard** (`/`): Balance, P&L, positions, Intel Feed with 5-section macro panel
- **Trade Room** (`/trade-room`): 7 agent cards, 5-section macro panel (metals, forex, futures, key metrics, yahoo data), live feed
- **History** (`/history`): Equity curve (anchored to real balance), stat cards (Population B), monthly breakdown

## Safety

- FLO-85: Hard gate prevents opposing positions (BUY + SELL simultaneously)
- FLO-116: EA is pure executor — 9999-pip BE/trailing triggers (never fires). Floki manages via adjust_trade.
- FLO-118: SafetyChecker blocking removed — Floki manages own risk. Sage advises via session memory.
- Market hours + MT5 connection checks remain active.

## Disclaimer

1. **Always test in DEMO mode first**
2. **Start with small capital** in LIVE mode
3. **Monitor via Discord and Trade Room** during initial weeks
4. **Keep MT5 running** on VPS or dedicated machine

---

**Trading involves risk of capital loss. Use at your own risk.**
