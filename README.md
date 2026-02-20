# 🤖 FlokiWatch — XAU/USD Trading Bot

Fully automated trading bot for XAU/USD (Gold) using technical analysis, news sentiment, machine learning, and a 5-pillar "Central Brain" decision engine.

## 📋 Overview

The bot operates **100% autonomously** on MetaTrader 5:
- Opens trades automatically based on multi-pillar analysis
- Manages positions (breakeven, trailing stop)
- Closes trades automatically (TP/SL/trailing)
- Sends real-time alerts to Discord

**Just monitor via Discord and the FlokiWatch dashboard!**

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     FLOKIWATCH CENTRAL BRAIN                     │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌────────────┐ ┌────────────┐ ┌────────────┐ ┌────────────┐   │
│  │  TECHNICAL │ │  MOMENTUM  │ │    NEWS    │ │  CALENDAR  │   │
│  │   (35%)    │ │   (20%)    │ │   (20%)    │ │   (10%)    │   │
│  │            │ │            │ │            │ │            │   │
│  │ - EMAs     │ │ - ADX/DI   │ │ - Headlines│ │ - Events   │   │
│  │ - RSI/MACD │ │ - Volume   │ │ - DXY/VIX  │ │ - Phases   │   │
│  │ - Bollinger│ │ - Breakout │ │ - Yields   │ │ - Bias     │   │
│  └─────┬──────┘ └─────┬──────┘ └─────┬──────┘ └─────┬──────┘   │
│        │              │              │              │           │
│        └──────────────┴──────┬───────┴──────────────┘           │
│                              │                                   │
│                    ┌─────────┴─────────┐                        │
│                    │   ML ENSEMBLE     │                        │
│                    │      (15%)        │                        │
│                    │ XGB+LGB+CatBoost  │                        │
│                    │   H1 + H4 blend   │                        │
│                    └─────────┬─────────┘                        │
│                              ▼                                   │
│                    ┌───────────────────┐                        │
│                    │  SCENARIO ENGINE  │                        │
│                    │  Dynamic weights  │                        │
│                    │  Score 0-100      │                        │
│                    └─────────┬─────────┘                        │
│                              ▼                                   │
│                    ┌───────────────────┐                        │
│                    │  GPT VALIDATOR    │                        │
│                    │  Confidence ±15   │                        │
│                    └─────────┬─────────┘                        │
│                              ▼                                   │
│                    ┌───────────────────┐                        │
│                    │  SAFETY CHECKS    │                        │
│                    │  + Volatility     │                        │
│                    │    Guard          │                        │
│                    └─────────┬─────────┘                        │
│                              ▼                                   │
│                    ┌───────────────────┐                        │
│                    │  RISK MANAGER     │──────────► MT5         │
│                    │  ATR-based SL/TP  │                        │
│                    └───────────────────┘                        │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

## 📁 Project Structure

```
flokiwatch/
├── main.py                 # Main bot loop
├── config.py               # Configuration (loads from .env)
├── central_brain.py        # 5-pillar decision engine
├── confluence.py           # Legacy confluence system (fallback)
├── risk_manager.py         # Position sizing, SL/TP calculation
├── executor.py             # MT5 order execution
├── monitor.py              # Position monitoring (breakeven, trailing)
├── safety_checks.py        # Safety validations
├── volatility_guard.py     # Extreme volatility detection
├── technical_analyzer.py   # Technical indicators
├── momentum_detector.py    # Momentum analysis
├── ml_predictor.py         # ML ensemble predictions
├── news_score_hybrid.py    # GPT-powered news scoring
├── economic_calendar.py    # Economic calendar integration
├── gpt_confidence.py       # GPT confidence validator
├── support_resistance.py   # S/R zone detection
├── alerts.py               # Discord notifications
├── db_writer.py            # SQLite history database
├── state_writer.py         # Dashboard state file
├── dashboard/              # FlokiWatch web dashboard
├── scripts/                # Training & analysis scripts
├── models/                 # ML model configs (JSON)
├── data/                   # Runtime data (gitignored)
└── logs/                   # Log files (gitignored)
```

## ⚙️ Setup

### 1. Clone and Install

```bash
git clone https://github.com/hhvivaldi/flokiwatch.git
cd flokiwatch
pip install -r requirements.txt
```

### 2. Configure Environment

Copy `.env.example` to `.env` and fill in your credentials:

```bash
cp .env.example .env
```

Required variables:
- `MT5_ACCOUNT` — Your MT5 account number
- `MT5_PASSWORD` — Your MT5 password
- `MT5_SERVER` — Broker server (e.g., ICMarkets-Demo)
- `MT5_TERMINAL_PATH` — Path to terminal64.exe
- `DISCORD_WEBHOOK_URL` — Discord webhook for alerts
- `OPENAI_API_KEY` — OpenAI API key for GPT features

### 3. Configure MT5

1. Open MetaTrader 5
2. Tools → Options → Expert Advisors
3. Enable "Allow algorithmic trading"
4. Enable "Allow DLL imports"

### 4. Train ML Models (Optional)

Models are included, but to retrain:

```bash
python scripts/collect_training_data.py
python scripts/train_ensemble.py
```

## 🚀 Usage

### Start the Bot

```bash
python main.py
```

The bot will:
1. Connect to MT5
2. Run analysis every 5 minutes
3. Execute trades when conditions are met
4. Monitor positions every 10 seconds
5. Send Discord alerts for all events

### Start the Dashboard

```bash
cd dashboard
python server.py
```

Access at `http://localhost:5000`

## 📊 Decision System

### Score Thresholds

| Score | Decision | Action |
|-------|----------|--------|
| ≥ 65 | BUY | ✅ Opens BUY position |
| 35-65 | HOLD | ⏸️ No action |
| ≤ 35 | SELL | ✅ Opens SELL position |

### Confidence Gate

Trades only execute if confidence ≥ 55%. Lower confidence = forced HOLD.

### Scenario Multipliers

The brain adjusts weights based on detected scenarios:
- `alinhamento_perfeito` (1.15×) — All pillars agree
- `momentum_forte` (1.10×) — Strong momentum detected
- `lateralizacao` (0.85×) — Ranging market
- `sinais_conflitantes` (0.80×) — Conflicting signals
- `volatilidade_extrema` (0.00×) — Extreme volatility block

## 🛡️ Safety Features

The bot **blocks trades** when:
- ❌ MT5 disconnected
- ❌ Market closed (weekend, daily pause 21:00-22:00 UTC)
- ❌ 3+ consecutive losses (24h pause)
- ❌ 3+ open positions
- ❌ Daily loss > 6%
- ❌ Extreme volatility (>1.8% M5 candle)
- ❌ During high-impact news release
- ❌ Spread > 5 pips

## 💰 Risk Management

### Position Sizing

```
Risk per trade: 2% of capital
Lot size = (Capital × 2%) / (SL_pips × $10)
```

### Stop Loss / Take Profit

ATR-based (Average True Range):
- **SL**: 1.5 × ATR (min 150, max 800 pips)
- **TP**: 3.0 × ATR

### Position Management

1. **Breakeven**: SL moves to entry at 0.7 × SL distance profit
2. **Trailing**: Activates at 0.7 × SL distance, trails at 0.7 × ATR
3. **Max Duration**: Auto-close after 24h if profit < 5 pips

## 📱 Discord Alerts

- 🤖 Bot started/stopped
- 🟢 BUY signal detected
- 🔴 SELL signal detected
- ✅ Order executed
- 🔒 Breakeven activated
- � Trailing stop updated
- � Trade closed (TP/SL/Trailing)
- ⛔ Signal blocked (safety)
- 💓 Hourly heartbeat (when idle)
- ⚠️ Critical errors

## 📈 Performance

Backtest results (Jan-Feb 2026):

| Metric | In-Sample | Out-of-Sample |
|--------|-----------|---------------|
| Trades | 33 | 56 |
| Win Rate | 81.8% | 67.9% |
| Profit Factor | 3.53 | 1.62 |
| Max Drawdown | $137 | $123 |

## ⚠️ Disclaimer

1. **Always test in DEMO mode first**
2. **Start with small capital** in LIVE mode
3. **Monitor via Discord** during initial weeks
4. **Do not manually modify trades** — let the bot manage
5. **Keep MT5 running** on VPS or dedicated machine

---

**Trading involves risk of capital loss. Use at your own risk.**
