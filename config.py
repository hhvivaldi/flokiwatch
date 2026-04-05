"""
XAU/USD TRADING BOT CONFIGURATION
Fill in your details before running
"""

import os
from dotenv import load_dotenv
load_dotenv(override=True)

# ============================================================================
# MT5 ACCOUNT (loaded from .env)
# ============================================================================
MT5_ACCOUNT = int(os.environ.get("MT5_ACCOUNT", "0"))
MT5_PASSWORD = os.environ.get("MT5_PASSWORD", "")
MT5_SERVER = os.environ.get("MT5_SERVER", "ICMarkets-Demo")
MT5_TERMINAL_PATH = os.environ.get("MT5_TERMINAL_PATH", r"C:\Program Files\MetaTrader 5\terminal64.exe")

# ============================================================================
# RISK PARAMETERS
# ============================================================================
INITIAL_BALANCE = 1000  # USD - Initial account balance for dashboard/history calculations
CAPITAL_INICIAL = INITIAL_BALANCE  # USD - Initial account capital
RISK_PER_TRADE = 2.0  # % - Maximum risk per trade (1-2% recommended)
MAX_DAILY_LOSS = 6.0  # % - Maximum daily loss (for the bot)

# Stop Loss and Take Profit
STOP_LOSS_ATR_MULT = 1.5  # ATR multiplier for SL
TAKE_PROFIT_1_ATR_MULT = 3.0  # ATR multiplier for TP1 (ratio 1:2 vs SL)
TAKE_PROFIT_2_ATR_MULT = 4.5  # ATR multiplier for TP2 (ratio 1:3 vs SL)

# SL limits in PIPS (protection — ATR decides the actual value)
MIN_SL_PIPS = 50  # Minimum SL: 50 pips (calm market)
MAX_SL_PIPS = 800  # Maximum SL: 800 pips (effectively no cap — ATR decides)

# ============================================================================
# DECISION THRESHOLDS
# ============================================================================
STRONG_BUY_THRESHOLD = 70  # Score > 70 = STRONG_BUY
BUY_THRESHOLD = 65  # Score 65-70 = BUY
WEAK_BUY_THRESHOLD = 55  # Score 55-65 = WEAK_BUY (treated as HOLD)
NEUTRAL_LOW = 45  # Score 45-55 = HOLD
WEAK_SELL_THRESHOLD = 35  # Score 35-45 = WEAK_SELL (treated as HOLD)
SELL_THRESHOLD = 35  # Score 30-35 = SELL (< 35 and >= 30)
STRONG_SELL_THRESHOLD = 30  # Score < 30 = STRONG_SELL

# ============================================================================
# ML ENSEMBLE — FLO-187: master switch (disable without deleting code)
# ============================================================================
ML_ENABLED = os.environ.get("ML_ENABLED", "false").lower() in ("true", "1", "yes")

# ============================================================================
# CONFLUENCE WEIGHTS
# ============================================================================
WEIGHT_TECHNICAL = 0.45  # 45% - Technical Analysis
WEIGHT_NEWS = 0.40  # 40% - Hybrid News
WEIGHT_ML = 0.15  # 15% - Machine Learning

# When ML is ignored (prob < threshold)
WEIGHT_TECHNICAL_NO_ML = 0.529  # 52.9% (45/85)
WEIGHT_NEWS_NO_ML = 0.471  # 47.1% (40/85)

# ML Confidence
ML_MIN_PROBABILITY = 0.55  # Minimum probability to include ML

# ============================================================================
# SAFETY CHECKS
# ============================================================================
MAX_POSITIONS = 3  # Maximum simultaneous open positions
MIN_MINUTES_BETWEEN_TRADES = 45  # Fallback default (anti-overtrading)
MIN_MINUTES_AFTER_TRAILING = 30   # Trailing close → faster re-entry
MIN_MINUTES_AFTER_SL = 45         # SL close → more caution
MAX_CONSECUTIVE_LOSSES = 3  # Maximum consecutive losses before pausing
PAUSE_AFTER_LOSSES_HOURS = 24  # Hours of pause after consecutive losses

# Smart Pyramid: allow 2nd position in same direction ONLY if 1st is in profit
PYRAMID_MIN_PROFIT_PERCENT = 0.3  # Minimum profit (%) on existing position to allow reinforcement

# XAU/USD market hours (UTC)
# Gold trades: Sunday 22:00 UTC → Friday 21:00 UTC
# Daily pause: 21:00-22:00 UTC (Mon-Thu)
# XAU/USD market hours (UTC). CME gold closes 5pm ET = 21:00 UTC winter / 21:00 UTC summer
# (both ET and broker EET shift together, so UTC hours stay constant year-round).
# Override via env var if your broker uses non-standard hours.
MARKET_DAILY_CLOSE_HOUR = int(os.environ.get("MARKET_DAILY_CLOSE_HOUR", "21"))
MARKET_DAILY_OPEN_HOUR = int(os.environ.get("MARKET_DAILY_OPEN_HOUR", "22"))
MARKET_CLOSE_BUFFER_MINUTES = 60  # Don't open new positions 60 min before close (backtest: 64% losses were gaps)
MARKET_OPEN_BUFFER_MINUTES = 60   # Don't open new positions in 1st hour after open (22:00-23:00 UTC)

# FLO-208: Market holidays — XAU/USD closed all day (no trading)
MARKET_HOLIDAYS = {
    "2026-01-01": "New Year's Day",
    "2026-04-03": "Good Friday",
    "2026-05-25": "Memorial Day",
    "2026-07-04": "Independence Day",
    "2026-09-07": "Labor Day",
    "2026-11-26": "Thanksgiving Day",
    "2026-12-25": "Christmas Day",
}

# Maximum open position time
MAX_POSITION_HOURS = 24  # Close position after 24h if profit < 5 pips
MAX_POSITION_MIN_PROFIT_PIPS = 5  # Minimum profit to keep position

# Maximum drawdown per position
MAX_POSITION_DRAWDOWN_PIPS = 1000  # Emergency safety net: only triggers if broker SL fails (max SL ~300 pips)

# Position management mode
FLOKI_MANAGES_POSITION = True   # True = Floki actively manages open trades, EA acts as wide safety net

# Trailing Stop (2 phases) — fixed values as fallback
BREAKEVEN_TRIGGER_PIPS = 100    # Phase 1: move SL to entry after +100 pips (fallback)
TRAILING_TRIGGER_PIPS = 150     # Phase 2: activate trailing after +150 pips (fallback)
TRAILING_DISTANCE_PIPS = 100    # Trailing: SL stays 100 pips behind maximum (fallback)

# EA safety net parameters — current/tight behavior
EA_TIGHT_BREAKEVEN_TRIGGER_PIPS = BREAKEVEN_TRIGGER_PIPS
EA_TIGHT_TRAILING_TRIGGER_PIPS = TRAILING_TRIGGER_PIPS
EA_TIGHT_TRAILING_DISTANCE_PIPS = TRAILING_DISTANCE_PIPS
EA_TIGHT_MAX_DRAWDOWN_PIPS = MAX_POSITION_DRAWDOWN_PIPS

# Dynamic Trailing Stop (ATR-based — preferred)
BREAKEVEN_ATR_MULT = 0.5        # Breakeven trigger = 0.5 × SL distance (changed from 0.7 after backtest)
TRAILING_ATR_MULT = 0.7         # Trailing trigger = 0.7 × SL distance (activate earlier)
TRAILING_DISTANCE_ATR_MULT = 0.7  # Trailing distance = 0.7 × ATR

# EA safety net parameters — Floki-managed wide behavior
FLOKI_BREAKEVEN_ATR_MULT = 0.8      # Breakeven trigger = 0.8 × SL distance when Floki manages position
FLOKI_TRAILING_TRIGGER_PIPS = 500   # Wide trailing trigger to keep EA mostly passive
FLOKI_TRAILING_DISTANCE_PIPS = 300  # Wide trailing distance to avoid cutting winners early
FLOKI_MAX_DRAWDOWN_PIPS = MAX_POSITION_DRAWDOWN_PIPS

# Scheduling caps with open position
FLOKI_MAX_CHECK_WITH_POSITION = 10   # Max minutes between checks when a position is open
FLOKI_FALLBACK_CHECK_WITH_POSITION = 3  # Fallback minutes if Floki forgets set_next_check with open position

# ADJUST_TRADE rate limit
MAX_ADJUSTMENTS_PER_HOUR = 3  # Max successful ADJUST_TRADE per trade per rolling hour

# ============================================================================
# DISCORD WEBHOOK (loaded from .env)
# ============================================================================
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
DISCORD_BOT_NAME = "XAU/USD Trading Bot"
DISCORD_WEBHOOK_SIGNALS = os.environ.get("DISCORD_WEBHOOK_SIGNALS", "")
DISCORD_WEBHOOK_DASHBOARD = os.environ.get("DISCORD_WEBHOOK_DASHBOARD", "")
DISCORD_WEBHOOK_BRAIN = os.environ.get("DISCORD_WEBHOOK_BRAIN", "")
DISCORD_WEBHOOK_TRADES = os.environ.get("DISCORD_WEBHOOK_TRADES", "")
DISCORD_WEBHOOK_STATUS = os.environ.get("DISCORD_WEBHOOK_STATUS", "")
DISCORD_WEBHOOK_DAILY = os.environ.get("DISCORD_WEBHOOK_DAILY", "")
DISCORD_WEBHOOK_WEEKLY = os.environ.get("DISCORD_WEBHOOK_WEEKLY", "")
DISCORD_WEBHOOK_MONTHLY = os.environ.get("DISCORD_WEBHOOK_MONTHLY", "")
DISCORD_WEBHOOK_BACKTEST = os.environ.get("DISCORD_WEBHOOK_BACKTEST", "")
DISCORD_WEBHOOK_ERRORS = os.environ.get("DISCORD_WEBHOOK_ERRORS", "")
DISCORD_WEBHOOK_CHANGELOG = os.environ.get("DISCORD_WEBHOOK_CHANGELOG", "")

# New agent-specific webhooks (FLO-78)
DISCORD_WEBHOOK_FLOKI = os.environ.get("DISCORD_WEBHOOK_FLOKI", "")
DISCORD_WEBHOOK_REX = os.environ.get("DISCORD_WEBHOOK_REX", "")
DISCORD_WEBHOOK_LUNA = os.environ.get("DISCORD_WEBHOOK_LUNA", "")
DISCORD_WEBHOOK_ECHO = os.environ.get("DISCORD_WEBHOOK_ECHO", "")
DISCORD_WEBHOOK_SIMBA = os.environ.get("DISCORD_WEBHOOK_SIMBA", "")
DISCORD_WEBHOOK_SAGE = os.environ.get("DISCORD_WEBHOOK_SAGE", "")
DISCORD_WEBHOOK_BACKTESTS = os.environ.get("DISCORD_WEBHOOK_BACKTESTS", "")
DISCORD_WEBHOOK_ANNOUNCEMENTS = os.environ.get("DISCORD_WEBHOOK_ANNOUNCEMENTS", "")

# ============================================================================
# SYMBOL AND TIMEFRAME
# ============================================================================
SYMBOL = "XAUUSD"
TIMEFRAME = "H1"  # Main timeframe
ANALYSIS_BARS = 100  # Bars for analysis

# ============================================================================
# MAGIC NUMBER (bot ID)
# ============================================================================
MAGIC_NUMBER = 234000  # Unique identifier for bot orders

# ============================================================================
# OPERATION MODE
# ============================================================================
# "DRY_RUN" = Pure simulation (logic only, does not execute orders)
# "DEMO"    = MT5 demo (real execution with fake money - tests EVERYTHING)
# "LIVE"    = MT5 real (real execution with real money)
TRADING_MODE = "DEMO"

# Backward compatibility
DRY_RUN = (TRADING_MODE == "DRY_RUN")

# ============================================================================
# LOGGING
# ============================================================================
LOG_DIR = "logs"
LOG_LEVEL = "DEBUG"  # DEBUG for DRY_RUN (see full brain reasoning). Change to INFO in LIVE
VERBOSE_NEWS_LOG = True  # True = detailed log of News Score inputs (headlines, DXY, Yields, VIX)

# ============================================================================
# INTERVALS
# ============================================================================
ANALYSIS_INTERVAL_SECONDS = 60  # 1 minute between analyses
MONITOR_INTERVAL_SECONDS = 10  # 10s between position monitoring (when positions are open)
NEWS_CACHE_MINUTES = 30  # News cache for 30 minutes

# ============================================================================
# OPERATIONAL DASHBOARD (state file)
# ============================================================================
DASHBOARD_STATE_FILE = "data/bot_state.json"
HISTORY_DB_PATH = "data/history.db"

# ============================================================================
# STARTUP OPTIMIZATIONS
# ============================================================================
STARTUP_SKIP_THRESHOLD_MINUTES = 30

# ============================================================================
# XAU/USD SPECIFIC
# ============================================================================
PIP_VALUE_PER_LOT = 10.0  # $10 per pip for 1 standard lot
MIN_LOT_SIZE = 0.01  # Minimum lot
MAX_LOT_SIZE = 0.02  # Maximum lot (ultra conservative - ~$3 risk per trade)
LOT_STEP = 0.01  # Lot increment

# Maximum slippage
MAX_SLIPPAGE_PIPS = 20  # Maximum accepted deviation

# ============================================================================
# CENTRAL BRAIN
# ============================================================================
USE_CENTRAL_BRAIN = True  # True = use Central Brain, False = use confluence.py (fallback)

# Base Brain weights (dynamically adjusted per scenario)
BRAIN_WEIGHT_TECHNICAL = 0.35
BRAIN_WEIGHT_ML = 0.25
BRAIN_WEIGHT_MOMENTUM = 0.20
BRAIN_WEIGHT_NEWS = 0.20

# Brain decision thresholds
BRAIN_STRONG_BUY = 75
BRAIN_BUY = 65
BRAIN_SELL = 35
BRAIN_STRONG_SELL = 25

# Thresholds in ranging market (more conservative)
BRAIN_LATERAL_STRONG_BUY = 80
BRAIN_LATERAL_BUY = 70
BRAIN_LATERAL_SELL = 30
BRAIN_LATERAL_STRONG_SELL = 20

# Minimum confidence to execute trade
BRAIN_MIN_CONFIDENCE = 0  # FLO-200: Removed — Floki decides his own confidence threshold (was 55.0)

# Momentum Detector
MOMENTUM_ADX_PERIOD = 14
MOMENTUM_VOLUME_PERIOD = 20
MOMENTUM_BREAKOUT_LOOKBACK = 20

# MACD Divergence
MACD_DIVERGENCE_LOOKBACK = 20
MACD_DIVERGENCE_MIN_GAP = 5

# ============================================================================
# VISUAL FEATURES (visual context on chart)
# ============================================================================
VISUAL_FEATURES_ENABLED = False  # Disabled — backtest showed they worsen results (-$448 vs -$379)

# ============================================================================
# ECONOMIC CALENDAR (5th Pillar)
# ============================================================================
BRAIN_WEIGHT_CALENDAR = 0.10
CALENDAR_CACHE_MINUTES = 5
CALENDAR_PRE_EVENT_MINUTES = 30      # PRE_EVENT phase: <30 min before event
CALENDAR_DURING_MINUTES = 3          # DURING phase: 0-3 min after release
CALENDAR_POST_EVENT_MINUTES = 30     # POST_EVENT phase: 3-30 min after release
CALENDAR_JSON_PATH = os.environ.get("CALENDAR_JSON_PATH", r"C:\Users\Hermano\AppData\Roaming\MetaQuotes\Terminal\4C230EB692C96360065CCBB721258414\MQL5\Files\calendar_events.json")
CALENDAR_JSON_MAX_AGE_MINUTES = 15   # If JSON older than this, use fallback
MT5_SERVER_UTC_OFFSET = int(os.environ.get("MT5_SERVER_UTC_OFFSET", "2") or "2")
FCS_API_KEY = ""                     # Optional: fcsapi.com key for fallback
FRED_API_KEY = os.environ.get("FRED_API_KEY", "")  # Optional: fred.stlouisfed.org key for yields fallback

# ============================================================================
# VOLATILITY GUARD (Protection against Free Fall / Spike)
# ============================================================================
EXTREME_CANDLE_THRESHOLD_PERCENT = 1.8   # M5 candle with >1.8% move = EXTREME
EXTREME_CANCEL_THRESHOLD_PERCENT = 0.5   # Candle 2 < 0.5% → normalized (cancels extreme)
EXTREME_CONFIRM_THRESHOLD_PERCENT = 1.0  # Candle 2 >= 1.0% same direction → confirms cascade
COOLING_CONFIRMED_MINUTES = 90           # Cooling after confirmation (real cascade)
COOLING_AMBIGUOUS_MINUTES = 30           # Cooling if ambiguous (neither confirmed nor cancelled)
COOLING_MIN_CONFIDENCE = 70              # Minimum confidence to allow trade during cooling
COOLING_BREAKEVEN_TRIGGER_PIPS = 50      # Aggressive breakeven during cooling (normal: 100)
COOLING_TRAILING_TRIGGER_PIPS = 80       # Aggressive trailing trigger during cooling (normal: 150)
COOLING_TRAILING_DISTANCE_PIPS = 50      # Aggressive trailing distance during cooling (normal: 100)

# Early Exit — ABANDONED (tested Feb 2026: 70% would have recovered, PF 2.32→1.77)
# Kept for reference. Not used in live or backtest.
PYRAMID_EXIT_DRAWDOWN_PIPS = 80
PYRAMID_EXIT_COMBINED_DRAWDOWN_PCT = -0.15
PYRAMID_EXIT_SPEED_PIPS = 60
PYRAMID_EXIT_SPEED_MINUTES = 30
EXTREME_EXIT_ENABLED = False              # Disabled — abandoned feature
EXTREME_EXIT_MIN_LOSS_PIPS = 30
EXTREME_EXIT_GRACE_CANDLES = 1

# Deal History Retry (delays in seconds between attempts to find closing deal)
DEAL_HISTORY_RETRY_DELAYS = [5, 15]      # 3 attempts: immediate + 5s + 15s = 20s total

# ============================================================================
# M5 REVERSAL DETECTION (anti-lag filter)
# ============================================================================
M5_REVERSAL_CANDLES = 6                    # Last 6 M5 candles (~30 min) to detect reversal
M5_REVERSAL_MODERATE_THRESHOLD = 0.20      # Move ≥0.20% against direction → reduce confidence (-15)
M5_REVERSAL_STRONG_THRESHOLD = 0.40        # Move ≥0.40% against direction → block entry
M5_REVERSAL_CONFIDENCE_PENALTY = 15        # Confidence penalty for moderate reversal

# M5 Score Adjustment (influences brain score)
M5_SCORE_ADJUST_THRESHOLD = 0.15           # Minimum M5 move (%) to apply score adjustment
M5_SCORE_ADJUST_MAX = 7                    # Maximum score adjustment (±7 points)
M5_SCORE_ADJUST_FULL_MOVE = 0.40           # M5 move (%) that gives maximum adjustment (linear scale 0.15→0.40)
M5_SCORE_CONFIRM_BONUS = 2                 # Bonus when M5 confirms score direction

# ============================================================================
# SPREAD MONITORING
# ============================================================================
MAX_SPREAD_PIPS = 5.0                      # Maximum acceptable spread in pips (1 pip = $0.10)
SPREAD_RETRY_INTERVAL_SECONDS = 30         # Retry interval when spread is too high
SPREAD_MAX_RETRIES = 10                    # Max retries (10 × 30s = 5 minutes)

# ============================================================================
# HEARTBEAT DISCORD (alive signal when in HOLD)
# ============================================================================
HEARTBEAT_INTERVAL_MINUTES = 60          # Minimum interval between heartbeats
HEARTBEAT_SCORE_CHANGE_THRESHOLD = 8     # Score change that triggers full heartbeat (vs short)

# ============================================================================
# SUPPORT & RESISTANCE
# ============================================================================
SR_ZONE_MERGE_PIPS = 80                    # Distance in pips to merge nearby swing points into one zone
SR_ZONE_MERGE_PIPS_D1 = 150               # D1 zones are wider — larger merge radius
SR_ZONE_MAX_AGE_BARS = 500                 # Allow older zones to survive (H4 3-month, D1 6-month)
SR_MIN_TOUCHES = 2                         # Minimum touches to qualify as a zone
SR_LOOKBACK_H1 = 200                       # H1 bars to analyze (~8 trading days)
SR_LOOKBACK_H4 = 540                       # H4 bars to analyze (~3 months)
SR_LOOKBACK_D1 = 130                       # D1 bars to analyze (~6 months)
SR_FRACTAL_ORDER = 2                       # Fractal order (2 = 5-bar pattern)
SR_TOUCH_TOLERANCE_PIPS = 30              # Tolerance for touch detection
SR_CONFIDENCE_PENALTY_MAX = 0              # DISABLED — penalty blocks more winners than losers (zona_sr_forte scenario still active)
SR_CONFIDENCE_BONUS_MAX = 0                # DISABLED — marginal benefit overwhelmed by scenario cost
SR_TP_ADJUST_ENABLED = False               # DISABLED — backtest showed TP pull degrades PF by 0.16
SR_SL_ADJUST_ENABLED = False               # Extend SL past S/R zone (disabled: causes 3x DD)
SR_PENALTY_PROXIMITY_ATR = 0.5            # Penalty fires only within 0.5×ATR of zone (was 1.0)
SR_PENALTY_MIN_TOUCHES = 4                # Min touches for penalty to fire (was 3)
SR_SCENARIO_MIN_TOUCHES = 4               # Min touches for zona_sr_forte scenario
SR_SCENARIO_PROXIMITY_ATR_MULT = 0.5      # Price within 0.5×ATR of zone triggers scenario
SR_ZONES_JSON_PATH = os.environ.get("SR_ZONES_JSON_PATH", r"C:\Users\Hermano\AppData\Roaming\MetaQuotes\Terminal\4C230EB692C96360065CCBB721258414\MQL5\Files\sr_zones.json")

# ============================================================================
# GPT HEADLINE ANALYSIS
# ============================================================================
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
USE_GPT_HEADLINES = False
GPT_MODEL = "gpt-4o-mini"               # Model for headline analysis
GPT_HEADLINE_TIMEOUT = 15               # Timeout in seconds for GPT call
GPT_HEADLINE_TEMPERATURE = 0.1          # Low = more deterministic

# ============================================================================
# GPT CONFIDENCE VALIDATOR
# ============================================================================
USE_GPT_CONFIDENCE = bool(OPENAI_API_KEY and OPENAI_API_KEY != "sk-COLOQUE_SUA_KEY_AQUI")
GPT_CONFIDENCE_TIMEOUT = 12             # Timeout in seconds for GPT call
GPT_CONFIDENCE_TEMPERATURE = 0.1        # Low = more deterministic
GPT_CONFIDENCE_MAX_ADJUSTMENT = 15      # Maximum confidence adjustment (±)
GPT_CONFIDENCE_CACHE_THRESHOLD = 5      # Only re-call GPT if pillar changed ≥5 pts or scenario changed

# ============================================================================
# SIMBA WATCHER (Floki sleep gating)
# ============================================================================
SIMBA_TIMEOUT_SECONDS = int(os.environ.get("SIMBA_TIMEOUT_SECONDS", "10") or "10")
SIMBA_POLL_MINUTES = int(os.environ.get("SIMBA_POLL_MINUTES", "60") or "60")
SIMBA_MAX_SLEEP_DEFAULT_MINUTES = int(os.environ.get("SIMBA_MAX_SLEEP_DEFAULT_MINUTES", "120") or "120")

# ============================================================================
# REX VALIDATOR (debate partner)
# ============================================================================
REX_MODEL = os.environ.get("REX_MODEL", "gpt-4o")

# ============================================================================
# MULTI-TF TREND CONFIRMATION
# ============================================================================
MTF_TREND_ENABLED = True                 # Enable Multi-TF trend check
MTF_TREND_ALIGN_BONUS = 10               # Confidence bonus when trade aligns with D1+H4 trend
MTF_TREND_CONFLICT_PENALTY = 10          # Confidence penalty when trade conflicts with D1+H4 trend (reduced from 20)
MTF_EMA_PERIOD = 50                      # EMA period for trend detection (price vs EMA50)

# ============================================================================
# VOLUME GATE
# ============================================================================
VOLUME_GATE_ENABLED = True               # Enable volume gate penalties
VOLUME_GATE_MODERATE_THRESHOLD = 0.5     # Volume < 0.5x average triggers moderate penalty
VOLUME_GATE_MODERATE_PENALTY = 15        # Confidence penalty for moderate low volume
VOLUME_GATE_SEVERE_THRESHOLD = 0.3       # Volume < 0.3x average triggers severe penalty
VOLUME_GATE_SEVERE_PENALTY = 25          # Confidence penalty for severe low volume

# ============================================================================
# MACD DIVERGENCE
# ============================================================================
MACD_DIVERGENCE_ADJUSTMENT = 15          # Score adjustment for MACD divergence (reduced from 25)

# ============================================================================
# EA BRIDGE (Python ↔ MT5 EA Communication)
# ============================================================================
USE_EA_BRIDGE = True                      # True = EA handles breakeven/trailing, False = Python handles (enabled after Phase 1 testing passed)
EA_STALE_THRESHOLD_SECONDS = 120          # If ea_status.json older than this, fall back to direct API (120s tolerates recompile gaps)
BRAIN_SIGNAL_JSON_PATH = os.environ.get("BRAIN_SIGNAL_JSON_PATH", r"C:\Users\Hermano\AppData\Roaming\MetaQuotes\Terminal\4C230EB692C96360065CCBB721258414\MQL5\Files\brain_signal.json")
EA_STATUS_JSON_PATH = os.environ.get("EA_STATUS_JSON_PATH", r"C:\Users\Hermano\AppData\Roaming\MetaQuotes\Terminal\4C230EB692C96360065CCBB721258414\MQL5\Files\ea_status.json")

# ============================================================================
# AI AGENT (Claude-based decision maker)
# ============================================================================
USE_AI_AGENT = True                       # True = enable AI Agent, False = Brain-only mode
AI_AGENT_MODE = "active"                  # shadow = Agent decides but Brain executes | gate = Agent controls entry | full = Agent controls all
AI_AGENT_MODEL = os.environ.get("AI_AGENT_MODEL", "")  # Legacy (Anthropic) model name; unused for Gemini
AI_AGENT_TIMEOUT = 240                    # Timeout in seconds for API calls
AI_AGENT_MAX_TOOL_CALLS = 40              # Max tool calls per decision (investigation + debate + execution)

# FLO-130: Floki model (migrated from Gemini to OpenAI GPT-5.4)
FLOKI_MODEL = os.environ.get("FLOKI_MODEL", "gpt-5.4")
FLOKI_CALL_INTERVAL = int(os.environ.get("FLOKI_CALL_INTERVAL", "300") or "300")

# ============================================================================
# SAGE PERFORMANCE AUDITOR (daily)
# ============================================================================
USE_SAGE_AUDITOR = True
SAGE_RUN_TIME_UTC = "21:00"

# Dedicated API key and model for Sage (stays on Gemini, separate from Floki's GPT-5.4)
SAGE_API_KEY = os.environ.get("SAGE_API_KEY", "")
SAGE_MODEL = os.environ.get("SAGE_MODEL", "gemini-3-flash-preview")

# FLO-194: Research Manager (Gemini — picks winner between Rex Bull and Rex Bear)
RESEARCH_MANAGER_MODEL = os.environ.get("RESEARCH_MANAGER_MODEL", "gemini-3-flash-preview")

# Intraday drawdown alerts (FLO-68)
SAGE_INTRADAY_DRAWDOWN_ALERT = float(os.environ.get("SAGE_INTRADAY_DRAWDOWN_ALERT", "-30"))   # dollars — alert if daily P&L <= this
SAGE_INTRADAY_LOSS_STREAK_ALERT = int(os.environ.get("SAGE_INTRADAY_LOSS_STREAK_ALERT", "3"))  # consecutive losses — alert if streak >= this

# NOTE: FLOKI_CALL_INTERVAL controls the Agent's preferred scheduling cadence,
# but the main loop analysis cadence remains governed by ANALYSIS_INTERVAL_SECONDS.

# ============================================================================
# ECHO NEWS SENTINEL (24/7 breaking news monitor)
# ============================================================================
ECHO_ENABLED = True                                                           # Master switch for Echo agent
ECHO_MODEL = os.environ.get("ECHO_MODEL", "mimo-v2-flash")                   # Classification model (MiMo-V2-Flash via Xiaomi API)
ECHO_API_KEY = os.environ.get("ECHO_API_KEY", os.environ.get("LUNA_API_KEY", ""))    # Shared Xiaomi API key; falls back to LUNA_API_KEY
ECHO_API_BASE = os.environ.get("ECHO_API_BASE", "https://api.xiaomimimo.com/v1")    # MiMo API base URL
ECHO_MAX_WAKES_PER_HOUR = int(os.environ.get("ECHO_MAX_WAKES_PER_HOUR", "2"))       # Safety cap on CRITICAL → Simba wake
ECHO_SCAN_INTERVAL_SECONDS = int(os.environ.get("ECHO_SCAN_INTERVAL_SECONDS", "300"))       # 5 min for direct RSS feeds
ECHO_GOOGLE_SCAN_INTERVAL_SECONDS = int(os.environ.get("ECHO_GOOGLE_SCAN_INTERVAL_SECONDS", "600"))  # 10 min for Google News feeds
ECHO_COOLDOWN_MINUTES = int(os.environ.get("ECHO_COOLDOWN_MINUTES", "30"))           # Dedup window — same headline ignored within this window
ECHO_DAILY_COST_CAP = float(os.environ.get("ECHO_DAILY_COST_CAP", "1.00"))          # Daily cost cap in USD (safety)
ECHO_MAX_AGE_HOURS_DIRECT = float(os.environ.get("ECHO_MAX_AGE_HOURS_DIRECT", "6"))   # Direct feeds: max 6h old
ECHO_MAX_AGE_HOURS_GOOGLE = float(os.environ.get("ECHO_MAX_AGE_HOURS_GOOGLE", "12"))  # Google News: max 12h old
DISCORD_WEBHOOK_ECHO = os.environ.get("DISCORD_WEBHOOK_ECHO", "")                   # Echo alerts Discord channel

# ============================================================================
# LUNA MACRO ANALYST (AI-powered macro environment analysis)
# ============================================================================
LUNA_ENABLED = True                                                                  # Master switch for Luna agent
LUNA_MODEL = "mimo-v2-flash"                                                         # MiMo-V2-Flash for macro interpretation
LUNA_API_KEY = os.environ.get("LUNA_API_KEY", "")                                    # MiMo API key
LUNA_API_BASE = "https://api.xiaomimimo.com/v1"                                      # MiMo API base URL
LUNA_SCAN_INTERVAL_SECONDS = int(os.environ.get("LUNA_SCAN_INTERVAL_SECONDS", "900"))        # 15 min during market hours
LUNA_SCAN_INTERVAL_CLOSED = int(os.environ.get("LUNA_SCAN_INTERVAL_CLOSED", "1800"))         # 30 min when market closed
LUNA_DAILY_COST_CAP = float(os.environ.get("LUNA_DAILY_COST_CAP", "1.00"))                   # Daily cost cap in USD

# Rex Monitor (FLO-211)
REX_MONITOR_ENABLED = os.environ.get("REX_MONITOR_ENABLED", "true").lower() in ("true", "1", "yes")
REX_MONITOR_INTERVAL = int(os.environ.get("REX_MONITOR_INTERVAL", "1800"))                  # 30 min during market hours
REX_MONITOR_INTERVAL_CLOSED = int(os.environ.get("REX_MONITOR_INTERVAL_CLOSED", "3600"))    # 60 min when market closed

# ============================================================================
