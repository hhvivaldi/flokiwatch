"""
LUNA — Macro Analyst Agent (AI-powered via MiMo)
Reads the macro environment (DXY, VIX, Yields, Oil, S&P 500, Gold, Echo alerts,
economic calendar) and produces a structured brief for Floki.

Primary path: MiMo-V2-Flash API (OpenAI SDK with custom base_url)
Fallback: deterministic local analysis if API unavailable

Luna does NOT recommend entries or directions.  She describes the environment,
detects cross-asset patterns, and classifies risk.
"""

import json
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from logger import log

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
try:
    import config
except ImportError:
    config = None  # type: ignore[assignment]

LUNA_ENABLED = getattr(config, "LUNA_ENABLED", True)
LUNA_INTERVAL_SECONDS = int(getattr(config, "LUNA_INTERVAL_SECONDS", 900))  # 15 min
LUNA_API_KEY = os.environ.get("LUNA_API_KEY", getattr(config, "LUNA_API_KEY", ""))
LUNA_BASE_URL = "https://api.xiaomimimo.com/v1"
LUNA_MODEL = "mimo-v2-flash"
LUNA_DAILY_COST_CAP = float(getattr(config, "LUNA_DAILY_COST_CAP", 1.00))

DATA_DIR = Path(__file__).parent / "data"
BRIEF_FILE = DATA_DIR / "luna_brief.json"
COST_FILE = DATA_DIR / "luna_daily_cost.json"
MACRO_HISTORY_FILE = DATA_DIR / "macro_history.json"
MACRO_HISTORY_DAYS = 5  # Keep last 5 business days

# ---------------------------------------------------------------------------
# System Prompt
# ---------------------------------------------------------------------------

LUNA_SYSTEM_PROMPT = """You are Luna, a senior macro analyst with 15 years of experience at a gold-focused commodity desk. Your job is to read the macro environment and produce a structured brief for Floki, the portfolio manager who trades XAU/USD.

You are NOT a trader. You do not recommend entries, exits, or directions. You describe the ENVIRONMENT — what is happening around gold, what forces are in play, and what patterns you detect. Floki decides what to do with your analysis.

DATA YOU RECEIVE:
- DXY (US Dollar Index): value + 24h change
- VIX (Fear Index): value + 24h change
- Treasury Yields 10Y: value + 24h change
- Oil (Crude WTI): price + 24h change + 1h change
- S&P 500: value + 24h change
- Gold price: current price + 24h change + 1h change
- GLD ETF: price + volume + 24h change (gold proxy with real volume — high volume confirms conviction, low volume flags divergence risk)
- USD/CNY: exchange rate + 24h change (yuan weakness signals capital flight into gold; strengthening yuan reduces gold demand from China)
- Real Yields (TIPS 10Y, FRED DFII10): current value + change (higher real yields = gold less attractive as non-yielding asset; falling real yields = bullish gold)
- Fed Funds Rate (FRED FEDFUNDS): current value + change (rate cuts = dovish = bullish gold; rate hikes = hawkish = bearish gold)
- Breakeven Inflation 10Y (FRED T10YIE): current value + change (rising breakevens = inflation expectations up = bullish gold as inflation hedge)
- CPI All Urban (FRED CPIAUCSL): current value + change (rising CPI = inflation reality = bullish gold; falling CPI = less need for gold hedge)
- Echo classified news alerts: each has classification (CRITICAL/IMPORTANT/ROUTINE), gold_impact (BULLISH/BEARISH/NEUTRAL), and a summary. CRITICAL alerts indicate immediate market-moving events — factor them heavily. IMPORTANT alerts provide context. ROUTINE alerts can be ignored.
- Economic calendar: upcoming events with impact level and time

ENVIRONMENT CLASSIFICATION:
SAFE — Normal conditions. No unusual cross-asset stress. Gold trading on technicals.
Criteria: VIX < 20, DXY change < 1%, Oil change < 3%, no CRITICAL Echo alerts.

CAUTION — Elevated risk or opportunity. One or more macro forces creating unusual conditions. Floki should factor macro into decisions.
Criteria: VIX 20-30, OR DXY change > 1%, OR Oil change > 3%, OR IMPORTANT Echo alerts with gold impact, OR yields moving > 2%.

DANGER — Extreme macro stress. Multiple forces converging. High probability of large gold moves. Floki must prioritize risk management.
Criteria: VIX > 30, OR multiple CAUTION signals active simultaneously, OR CRITICAL Echo alerts, OR pattern detected (forced_liquidation, safe_haven_flow).

PATTERN DETECTION — Flag these specific patterns when ALL criteria are met:

forced_liquidation:
  VIX > 25 AND rising + Oil change > +3% in 24h + Gold change negative (falling) + S&P 500 change negative (falling)
  Meaning: institutions selling everything for cash. Gold drops despite being safe haven.
  Gold impact: SHORT-TERM BEARISH (forced selling), MEDIUM-TERM BULLISH (recovery).

safe_haven_flow:
  VIX rising (change positive) + DXY falling (change negative) + Gold rising (change positive)
  Meaning: flight from risk assets into gold. Classic safe haven demand.
  Gold impact: BULLISH.

news_price_divergence:
  Echo CRITICAL/IMPORTANT alerts are BULLISH but gold change is NEGATIVE (or vice versa)
  Meaning: market is not reacting to news as expected. Possible positioning or delayed reaction.
  Gold impact: WATCH — divergence often resolves violently.

dollar_gold_correlation_break:
  DXY change_24h and Gold change_24h are BOTH positive or BOTH negative (same direction)
  Normal: DXY up = Gold down (inverse correlation)
  Meaning: unusual force overriding normal correlation. Often geopolitical or central bank driven.
  Gold impact: INVESTIGATE — the unusual driver is the story.

OUTPUT FORMAT — Return ONLY valid JSON:
{
  "timestamp": "ISO 8601",
  "environment": "SAFE" | "CAUTION" | "DANGER",
  "risk_level": 1-10,
  "directional_bias": "BULLISH" | "BEARISH" | "NEUTRAL",
  "bias_confidence": 1-10,
  "key_factors": ["factor 1", "factor 2", "factor 3"],
  "patterns_detected": ["pattern_name"] or [],
  "pattern_details": {"pattern_name": "explanation with specific numbers"},
  "market_regime": "risk_on" | "risk_off" | "mixed" | "crisis",
  "summary": "2-3 sentences max.",
  "next_events": [{"event": "name", "time": "HH:MM UTC", "impact": "HIGH/MEDIUM/LOW"}],
  "data_snapshot": {
    "dxy": {"value": X, "change_pct": Y},
    "vix": {"value": X, "change_pct": Y},
    "yields_10y": {"value": X, "change_pct": Y},
    "oil": {"value": X, "change_pct": Y},
    "sp500": {"value": X, "change_pct": Y},
    "gold": {"price": X, "change_pct": Y},
    "gld": {"value": X, "volume": N, "change_pct": Y},
    "usdcny": {"value": X, "change_pct": Y},
    "real_yields": {"value": X, "change": Y},
    "fed_funds": {"value": X, "change": Y},
    "breakeven": {"value": X, "change": Y},
    "cpi": {"value": X, "change": Y}
  }
}

CALIBRATION RULES:
- risk_level: 1-3 = SAFE conditions, 4-6 = CAUTION conditions, 7-10 = DANGER conditions. Must be consistent with environment classification.
- bias_confidence: 1-3 = low (missing data or conflicting signals), 4-6 = medium (partial data alignment), 7-10 = high (all data points align). If any data source is null, cap bias_confidence at 5.
- Be specific. "VIX at 28.4, up 12% from yesterday" not "VIX is elevated."
- Every claim must reference a number from the data you received.
- If data is missing (null), say so. Do not invent values.
- Patterns require ALL criteria to be met. Do not flag partial matches.
- Keep summary under 3 sentences. Floki reads this every 15 minutes — be concise.

FACTOR ALIGNMENT:
When assessing directional bias and confidence, consider how many of the 5 major macro forces align:
- DXY direction (falling favors gold, rising pressures gold)
- VIX direction (rising = fear = gold demand, falling = calm = gold less needed)
- Yields direction (falling = gold attractive, rising = gold less attractive)
- Oil context (rising during crisis = geopolitical safe haven; rising without crisis = inflation pressure)
- Equities direction (S&P falling = risk off = gold demand; S&P rising = risk on)

When most factors point the same way, your confidence should be HIGH and your bias clear.
When factors conflict (e.g. war headlines bullish but yields rising bearish), your confidence should be LOW and your summary must explain the conflict.
The most dangerous scenario is when headlines say one thing but price action and macro factors say the opposite — this often means forced liquidation or delayed reaction. Always flag this.

TREND ANALYSIS:
Use macro_trend_5d data to distinguish escalating vs stabilizing conditions:
- Escalating trends (VIX rising 3+ days, DXY accelerating) increase risk_level
- Stabilizing trends (VIX falling from highs, yields flattening) decrease risk_level
- "VIX at 26.78" means different things if it came from 18 (escalating) vs from 35 (recovering)
- Always reference the trend direction in your summary when it contradicts the snapshot level

CORRELATION ANALYSIS:
When correlation breaks from historical norm (status = BROKEN), flag it and adjust interpretation.
A broken gold-DXY correlation (normally -0.7 to -0.9, now positive) often signals regime change:
forced liquidation, central bank intervention, or structural shift. "DXY falling = gold bullish"
may be WRONG during a correlation break. Always check correlation status before applying standard rules.

GLD ETF SENTIMENT:
GLD sentiment is a volume-based conviction indicator, NOT actual ETF flows.
ACCUMULATION = high volume + rising price (institutions buying with conviction).
DISTRIBUTION = high volume + falling price (institutions selling with conviction).
QUIET_BID = low volume + rising price (steady demand, no urgency).
QUIET = low volume + no clear direction.
This is a PROXY — it cannot measure actual fund inflows/outflows.

ECHO SENTIMENT AGGREGATE:
Echo sentiment aggregate shows the TREND of news coverage across 1h and 4h windows. When 80%+ of headlines point one direction, that's strong news confirmation. When sentiment is MIXED, headlines conflict — trust macro data more than news. Individual CRITICAL alerts still matter regardless of aggregate direction.

SAGE PERFORMANCE CONTEXT:
You receive historical performance insights from Sage showing Floki's actual win rates by session, direction, and day-of-week (last 14 days). Consider this when assessing risk:
- If current session has historically low win rate (< 35%), note it in your summary and increase risk_level
- If Sage flags a danger pattern matching current conditions, mention it
- This data reflects Floki's ACTUAL performance, not theoretical — respect it

REAL YIELD PROXY:
Use the "Real Yield proxy (live)" for intraday decisions. The FRED DFII10 value is end-of-day (previous close). The proxy = nominal yield minus breakeven inflation gives a live estimate using real-time ^TNX data."""


# ---------------------------------------------------------------------------
# Data Result
# ---------------------------------------------------------------------------

@dataclass
class LunaAnalysisResult:
    timestamp: str
    environment: str          # SAFE / CAUTION / DANGER
    risk_level: int           # 1-10
    directional_bias: str     # BULLISH / BEARISH / NEUTRAL
    bias_confidence: int      # 1-10
    key_factors: List[str] = field(default_factory=list)
    patterns_detected: List[str] = field(default_factory=list)
    pattern_details: Dict[str, str] = field(default_factory=dict)
    market_regime: str = "mixed"
    summary: str = ""
    next_events: List[Dict[str, str]] = field(default_factory=list)
    data_snapshot: Dict[str, Any] = field(default_factory=dict)
    macro_trend: Dict[str, Any] = field(default_factory=dict)
    correlations: Dict[str, Any] = field(default_factory=dict)
    source: str = "mimo"      # "mimo" or "local_fallback"
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Cost Tracker
# ---------------------------------------------------------------------------

def _load_daily_cost() -> Dict:
    try:
        if COST_FILE.exists():
            data = json.loads(COST_FILE.read_text(encoding="utf-8"))
            if data.get("date") == datetime.utcnow().strftime("%Y-%m-%d"):
                return data
    except Exception:
        pass
    return {"date": datetime.utcnow().strftime("%Y-%m-%d"), "total_usd": 0.0, "calls": 0}


def _save_daily_cost(cost_data: Dict) -> None:
    try:
        tmp_path = str(COST_FILE) + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(cost_data, f, indent=2)
        os.replace(tmp_path, str(COST_FILE))
    except Exception:
        pass


def _estimate_cost(input_tokens: int, output_tokens: int) -> float:
    """Estimate cost for MiMo-V2-Flash. Conservative estimate."""
    # MiMo pricing — adjust when official rates are published
    return (input_tokens * 0.15 + output_tokens * 0.60) / 1_000_000


# ---------------------------------------------------------------------------
# Data Collection
# ---------------------------------------------------------------------------

def _mt5_to_luna(mt5_entry: dict) -> dict:
    """Remap MT5 market_context entry to Luna's key format."""
    if not mt5_entry or not isinstance(mt5_entry, dict):
        return {}
    result = {"current": mt5_entry.get("bid"), "change_percent": mt5_entry.get("change_pct", 0)}
    if mt5_entry.get("position_in_range") is not None:
        result["position_in_range"] = mt5_entry["position_in_range"]
    return result


def _get_macro_data() -> Dict[str, Any]:
    """Fetch macro data from MT5 (correlated markets) + Yahoo/FRED (yields, GLD, FRED series)."""
    from news_score_hybrid import (
        get_yields_data,
        get_gld_data,
        get_real_yields,
        get_fed_funds_rate,
        get_breakeven_inflation,
        get_cpi_data,
        get_gld_weekly_flows,
    )

    # MT5 correlated markets (replaces Yahoo for DXY, VIX, Oil, S&P, USD/CNY)
    try:
        from market_context_fetcher import fetch_market_context
        mc = fetch_market_context()
    except Exception:
        mc = {}

    # Remap 5 overlapping instruments from MT5 → Luna format
    futures = mc.get("futures", {})
    indices = mc.get("indices", {})
    energy = mc.get("energy", {})
    forex = mc.get("forex", {})

    dxy = _mt5_to_luna(futures.get("DXY_M6"))
    vix = _mt5_to_luna(futures.get("VIX_J6"))
    oil = _mt5_to_luna(energy.get("XTIUSD"))
    sp500 = _mt5_to_luna(indices.get("US500"))
    usdcny = _mt5_to_luna(forex.get("USDCNH"))

    # Gold price from MT5 XAUUSD (replaces Yahoo GC=F which was 4.5h stale during intraday)
    gold = {}
    try:
        import MetaTrader5 as _mt5
        _gt = _mt5.symbol_info_tick("XAUUSD")
        if _gt and _gt.bid > 0:
            _gi = _mt5.symbol_info("XAUUSD")
            _pc = getattr(_gi, "session_close", 0) if _gi else 0
            _chg = round(((_gt.bid - _pc) / _pc) * 100, 2) if _pc and _pc > 0 else 0
            gold = {"current": round(_gt.bid, 2), "change_percent": _chg}
    except Exception:
        pass
    if not gold.get("current"):
        gold = _get_gold_data()  # fallback to Yahoo if MT5 unavailable

    # Yahoo/FRED data (no MT5 equivalent) — P1-7: individually wrapped
    yields = None
    try:
        yields = get_yields_data()
    except Exception as e:
        log.warning(f"LUNA: get_yields_data failed — {e}")

    gld = None
    try:
        gld = get_gld_data()
    except Exception as e:
        log.warning(f"LUNA: get_gld_data failed — {e}")

    real_yields = None
    try:
        real_yields = get_real_yields()
    except Exception as e:
        log.warning(f"LUNA: get_real_yields failed — {e}")

    fed_funds = None
    try:
        fed_funds = get_fed_funds_rate()
    except Exception as e:
        log.warning(f"LUNA: get_fed_funds_rate failed — {e}")

    breakeven = None
    try:
        breakeven = get_breakeven_inflation()
    except Exception as e:
        log.warning(f"LUNA: get_breakeven_inflation failed — {e}")

    cpi = None
    try:
        cpi = get_cpi_data()
    except Exception as e:
        log.warning(f"LUNA: get_cpi_data failed — {e}")

    gld_flows = None
    try:
        gld_flows = get_gld_weekly_flows()
    except Exception as e:
        log.warning(f"LUNA: get_gld_weekly_flows failed — {e}")

    # FLO-70: Sage performance insights
    sage_insights = None
    try:
        _sage_file = DATA_DIR / "sage_insights_for_luna.json"
        if _sage_file.exists():
            sage_insights = json.loads(_sage_file.read_text(encoding="utf-8"))
            if not isinstance(sage_insights, dict):
                sage_insights = None
    except Exception:
        pass

    # New from MT5: correlated metals, forex pairs, crypto
    metals = {}
    for sym in ("XAGUSD", "XPTUSD", "XPDUSD"):
        metals[sym] = _mt5_to_luna((mc.get("metals") or {}).get(sym))
    metals["gold_silver_ratio"] = (mc.get("metals") or {}).get("gold_silver_ratio")

    forex_pairs = {}
    for sym in ("EURUSD", "USDJPY", "USDCHF", "AUDUSD", "GBPUSD"):
        forex_pairs[sym] = _mt5_to_luna(forex.get(sym))
    forex_pairs["dollar_strength"] = forex.get("dollar_strength")

    btc = _mt5_to_luna((mc.get("crypto") or {}).get("BTCUSD"))

    return {
        "dxy": dxy,
        "vix": vix,
        "yields": yields,
        "oil": oil,
        "sp500": sp500,
        "gold": gold,
        "gld": gld,
        "usdcny": usdcny,
        "real_yields": real_yields,
        "fed_funds": fed_funds,
        "breakeven": breakeven,
        "cpi": cpi,
        "gld_flows": gld_flows,
        "sage_insights": sage_insights,
        "metals": metals,
        "forex": forex_pairs,
        "btc": btc,
    }


def _get_gold_data() -> Dict[str, Any]:
    """Fetch gold price + change via Yahoo Finance (GC=F)."""
    try:
        import yfinance as yf

        ticker = yf.Ticker("GC=F")
        hist_1h = ticker.history(period="5d", interval="1h")

        if hist_1h.empty or len(hist_1h) < 2:
            return {"current": None, "change_percent": 0, "change_1h_percent": 0, "error": "Gold data unavailable"}

        current = float(hist_1h["Close"].iloc[-1])
        prev_1h = float(hist_1h["Close"].iloc[-2])
        change_1h = ((current - prev_1h) / prev_1h) * 100

        daily = ticker.history(period="5d")
        if len(daily) >= 2:
            prev_day = float(daily["Close"].iloc[-2])
            change_24h = ((current - prev_day) / prev_day) * 100
        else:
            change_24h = change_1h

        return {
            "current": round(current, 2),
            "change_percent": round(change_24h, 2),
            "change_1h_percent": round(change_1h, 2),
        }
    except Exception as e:
        log.warning(f"LUNA GOLD: error fetching data — {e}")
        return {"current": None, "change_percent": 0, "change_1h_percent": 0, "error": str(e)}


def _get_echo_alerts() -> List[Dict[str, Any]]:
    """Load recent Echo alerts from echo_alerts.json."""
    alerts_file = DATA_DIR / "echo_alerts.json"
    try:
        if not alerts_file.exists():
            return []
        raw = json.loads(alerts_file.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return []
        # Last 20 alerts, most recent first
        return list(reversed(raw[-20:]))
    except Exception as e:
        log.warning(f"LUNA: error loading Echo alerts — {e}")
        return []


def _get_echo_aggregate() -> Optional[Dict[str, Any]]:
    """Load Echo sentiment aggregate from echo_aggregate.json (FLO-72)."""
    try:
        agg_file = DATA_DIR / "echo_aggregate.json"
        if agg_file.exists():
            data = json.loads(agg_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return None


def _get_calendar_events() -> List[Dict[str, str]]:
    """Load upcoming economic calendar events."""
    try:
        from economic_calendar import get_upcoming_events
        events = get_upcoming_events(max_events=5)
        if not isinstance(events, list):
            return []
        result = []
        for ev in events:
            if not isinstance(ev, dict):
                continue
            result.append({
                "event": ev.get("name", "Unknown"),
                "time": ev.get("time", ""),
                "impact": ev.get("importance", "MEDIUM"),
            })
        return result
    except Exception as e:
        log.warning(f"LUNA: error loading calendar — {e}")
        return []


# ---------------------------------------------------------------------------
# Build Data Context for AI
# ---------------------------------------------------------------------------

def _build_data_context(macro: Dict[str, Any], echo_alerts: List[Dict],
                        calendar_events: List[Dict]) -> str:
    """Build a text block of all macro data for the AI prompt."""
    lines = ["<macro_data>"]

    dxy = macro.get("dxy", {})
    lines.append(f"DXY: {dxy.get('current', 'N/A')} (24h change: {dxy.get('change_percent', 'N/A')}%)")

    vix = macro.get("vix", {})
    lines.append(f"VIX: {vix.get('current', 'N/A')} (24h change: {vix.get('change_percent', 'N/A')}%)")

    yields = macro.get("yields", {})
    lines.append(f"Yields 10Y: {yields.get('current', 'N/A')}% (24h change: {yields.get('change_percent', 'N/A')}%)")

    oil = macro.get("oil", {})
    lines.append(f"Oil WTI: ${oil.get('current', 'N/A')} (24h: {oil.get('change_percent', 'N/A')}%, 1h: {oil.get('change_1h_percent', 'N/A')}%)")

    sp500 = macro.get("sp500", {})
    lines.append(f"S&P 500: {sp500.get('current', 'N/A')} (24h: {sp500.get('change_percent', 'N/A')}%)")

    gold = macro.get("gold", {})
    lines.append(f"Gold: ${gold.get('current', 'N/A')} (24h: {gold.get('change_percent', 'N/A')}%, 1h: {gold.get('change_1h_percent', 'N/A')}%)")

    gld = macro.get("gld", {})
    lines.append(f"GLD ETF: ${gld.get('current', 'N/A')} (vol: {gld.get('volume', 'N/A')}, 24h: {gld.get('change_percent', 'N/A')}%)")

    usdcny = macro.get("usdcny", {})
    lines.append(f"USD/CNY: {usdcny.get('current', 'N/A')} (24h: {usdcny.get('change_percent', 'N/A')}%)")

    ry = macro.get("real_yields", {})
    lines.append(f"Real Yields (TIPS 10Y): {ry.get('current', 'N/A')}% (change: {ry.get('change', 'N/A')}, date: {ry.get('date', 'N/A')})")

    # FLO-76: Real yield intraday proxy = nominal yield - breakeven inflation
    nominal = yields.get("current")
    breakeven_val = macro.get("breakeven", {}).get("current")
    if nominal is not None and breakeven_val is not None:
        try:
            proxy = round(float(nominal) - float(breakeven_val), 2)
            lines.append(f"Real Yield proxy (live): {proxy}% (nominal {nominal}% - breakeven {breakeven_val}%)")
        except (TypeError, ValueError):
            pass

    ff = macro.get("fed_funds", {})
    lines.append(f"Fed Funds Rate: {ff.get('current', 'N/A')}% (change: {ff.get('change', 'N/A')}, date: {ff.get('date', 'N/A')})")

    be = macro.get("breakeven", {})
    lines.append(f"Breakeven Inflation 10Y: {be.get('current', 'N/A')}% (change: {be.get('change', 'N/A')}, date: {be.get('date', 'N/A')})")

    cpi = macro.get("cpi", {})
    lines.append(f"CPI (All Urban): {cpi.get('current', 'N/A')} (change: {cpi.get('change', 'N/A')}, date: {cpi.get('date', 'N/A')})")

    gld_flows = macro.get("gld_flows", {})
    if gld_flows.get("direction"):
        lines.append(f"GLD ETF sentiment (5d vs prev 5d): {gld_flows.get('direction')} | vol change {gld_flows.get('volume_change_pct', 'N/A')}% | price change {gld_flows.get('price_change_pct', 'N/A')}%")

    lines.append("</macro_data>")

    # FLO-123: Cross-market data from MT5
    metals = macro.get("metals", {})
    forex_pairs = macro.get("forex", {})
    btc = macro.get("btc", {})
    _has_cross = any(
        isinstance(v, dict) and v.get("current") is not None
        for v in [metals.get("XAGUSD"), metals.get("XPTUSD"), btc]
    )
    if _has_cross:
        lines.append("\n<correlated_markets>")
        for sym, label in [("XAGUSD", "Silver"), ("XPTUSD", "Platinum"), ("XPDUSD", "Palladium")]:
            m = metals.get(sym, {})
            if m.get("current") is not None:
                rng = f", range: {int(m['position_in_range'] * 100)}%" if m.get("position_in_range") is not None else ""
                chg = f"{(m.get('change_percent') or 0):+.2f}%" if m.get("change_percent") is not None else ""
                lines.append(f"{label}: ${m['current']} ({chg}{rng})")
        gsr = metals.get("gold_silver_ratio")
        if gsr:
            lines.append(f"Gold/Silver Ratio: {gsr}")
        fx_parts = []
        for sym, label in [("EURUSD", "EUR/USD"), ("USDJPY", "USD/JPY"), ("USDCHF", "USD/CHF"), ("AUDUSD", "AUD/USD"), ("GBPUSD", "GBP/USD")]:
            f = forex_pairs.get(sym, {})
            if f.get("current") is not None:
                fx_parts.append(f"{label}: {f['current']} ({(f.get('change_percent') or 0):+.2f}%)")
        if fx_parts:
            lines.append(" | ".join(fx_parts[:3]))
            if len(fx_parts) > 3:
                lines.append(" | ".join(fx_parts[3:]))
        ds = forex_pairs.get("dollar_strength")
        if ds:
            lines.append(f"Dollar Strength: {ds.upper()}")
        if btc.get("current") is not None:
            brng = f", range: {int(btc['position_in_range'] * 100)}%" if btc.get("position_in_range") is not None else ""
            lines.append(f"BTC: ${btc['current']:,.0f} ({(btc.get('change_percent') or 0):+.2f}%{brng})")
        lines.append("</correlated_markets>")

    # Macro trends (5-day rolling)
    trends = _build_macro_trends(macro)
    trend_text = _build_trend_context(trends)
    if trend_text:
        lines.append("\n" + trend_text)

    # Correlations (FLO-75)
    correlations = _build_correlations(macro)
    corr_text = _build_correlation_context(correlations)
    lines.append("\n" + corr_text)

    # FLO-70: Sage performance insights
    sage = macro.get("sage_insights")
    if sage and isinstance(sage, dict):
        lines.append("\n<sage_performance_context>")
        sp = sage.get("session_performance", {})
        if sp:
            sess_parts = []
            for s in ("ny", "london", "asian"):
                sv = sp.get(s)
                if sv and sv.get("sample", 0) >= 3:
                    sess_parts.append(f"{s.capitalize()} {int(sv['win_rate']*100)}% WR ({sv['sample']} trades)")
            if sess_parts:
                lines.append(f"Session: {', '.join(sess_parts)}")
        dp = sage.get("direction_performance", {})
        if dp:
            dir_parts = []
            for d in ("BUY", "SELL"):
                dv = dp.get(d)
                if dv and dv.get("sample", 0) >= 3:
                    dir_parts.append(f"{d} {int(dv['win_rate']*100)}% WR ({dv['sample']})")
            if dir_parts:
                lines.append(f"Direction: {', '.join(dir_parts)}")
        danger = sage.get("danger_patterns", [])
        if danger:
            lines.append(f"Danger: {'; '.join(danger[:3])}")
        best = sage.get("best_conditions", [])
        if best:
            lines.append(f"Best: {'; '.join(best[:3])}")
        lines.append("</sage_performance_context>")

    # Echo alerts
    critical_important = [a for a in echo_alerts
                          if a.get("classification") in ("CRITICAL", "IMPORTANT")]
    if critical_important:
        lines.append("\n<echo_alerts>")
        for a in critical_important[:10]:
            headline = a.get("representative_headline") or a.get("title", "")
            count = a.get("headline_count", 1)
            sources = a.get("sources", [])
            cluster_info = f" ({count} sources)" if count > 1 else ""
            lines.append(
                f"[{a.get('classification')}] {headline}{cluster_info} — "
                f"gold_impact: {a.get('gold_impact', 'N/A')} — {a.get('summary', '')}"
            )
        lines.append("</echo_alerts>")
    else:
        lines.append("\n<echo_alerts>No CRITICAL or IMPORTANT alerts.</echo_alerts>")

    # FLO-72: Echo sentiment aggregate
    echo_agg = _get_echo_aggregate()
    if echo_agg:
        lines.append("\n<echo_sentiment>")
        for window in ("1h", "4h"):
            w = echo_agg.get(window)
            if w and w.get("total", 0) > 0:
                t = w["total"]
                bull_pct = int(w.get("bullish", 0) / t * 100) if t else 0
                bear_pct = int(w.get("bearish", 0) / t * 100) if t else 0
                neut_pct = 100 - bull_pct - bear_pct
                lines.append(
                    f"Echo sentiment ({window}): {t} headlines — "
                    f"{bull_pct}% BULLISH, {bear_pct}% BEARISH, {neut_pct}% NEUTRAL. "
                    f"Dominant: {w.get('dominant', 'N/A')}"
                )
        lines.append("</echo_sentiment>")

    # Calendar
    if calendar_events:
        lines.append("\n<calendar>")
        for ev in calendar_events:
            lines.append(f"- {ev.get('event', '?')} at {ev.get('time', '?')} [{ev.get('impact', '?')}]")
        lines.append("</calendar>")

    return "\n".join(lines)


def _calc_real_yield_proxy(macro: Dict[str, Any]) -> Optional[float]:
    """Real yield proxy = nominal 10Y yield - breakeven inflation (FLO-76)."""
    nominal = macro.get("yields", {}).get("current")
    breakeven_val = macro.get("breakeven", {}).get("current")
    if nominal is not None and breakeven_val is not None:
        try:
            return round(float(nominal) - float(breakeven_val), 2)
        except (TypeError, ValueError):
            pass
    return None


def _build_data_snapshot(macro: Dict[str, Any]) -> Dict[str, Any]:
    """Build the data_snapshot dict for the output JSON."""
    def _snap(source: Dict, value_key: str = "current", change_key: str = "change_percent"):
        return {
            "value": source.get(value_key),
            "change_pct": source.get(change_key, 0),
        }

    gld = macro.get("gld", {})
    gld_flows = macro.get("gld_flows", {})
    usdcny = macro.get("usdcny", {})
    ry = macro.get("real_yields", {})
    ff = macro.get("fed_funds", {})
    be = macro.get("breakeven", {})
    cpi = macro.get("cpi", {})

    return {
        "dxy": _snap(macro.get("dxy", {})),
        "vix": _snap(macro.get("vix", {})),
        "yields_10y": _snap(macro.get("yields", {})),
        "oil": _snap(macro.get("oil", {})),
        "sp500": _snap(macro.get("sp500", {})),
        "gold": {
            "price": macro.get("gold", {}).get("current"),
            "change_pct": macro.get("gold", {}).get("change_percent", 0),
        },
        "gld": {
            "value": gld.get("current"),
            "volume": gld.get("volume"),
            "change_pct": gld.get("change_percent", 0),
        },
        "gld_flows": {
            "direction": gld_flows.get("direction"),
            "volume_change_pct": gld_flows.get("volume_change_pct"),
            "price_change_pct": gld_flows.get("price_change_pct"),
        },
        "usdcny": {
            "value": usdcny.get("current"),
            "change_pct": usdcny.get("change_percent", 0),
        },
        "real_yields": {
            "value": ry.get("current"),
            "change": ry.get("change"),
            "date": ry.get("date"),
            "proxy": _calc_real_yield_proxy(macro),
        },
        "fed_funds": {
            "value": ff.get("current"),
            "change": ff.get("change"),
            "date": ff.get("date"),
        },
        "breakeven": {
            "value": be.get("current"),
            "change": be.get("change"),
            "date": be.get("date"),
        },
        "cpi": {
            "value": cpi.get("current"),
            "change": cpi.get("change"),
            "date": cpi.get("date"),
        },
        "sage_insights": {
            "session_performance": (macro.get("sage_insights") or {}).get("session_performance"),
            "danger_patterns": (macro.get("sage_insights") or {}).get("danger_patterns"),
            "best_conditions": (macro.get("sage_insights") or {}).get("best_conditions"),
        } if macro.get("sage_insights") else None,
    }


# ---------------------------------------------------------------------------
# Macro History & Trends (FLO-74)
# ---------------------------------------------------------------------------

def _save_macro_snapshot(macro: Dict[str, Any]) -> None:
    """Save today's macro snapshot to macro_history.json (one entry per day)."""
    try:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        history = _load_macro_history()

        # Extract scalar values for history
        snapshot = {}
        for key in ("dxy", "vix", "yields", "oil", "sp500", "gold"):
            src = macro.get(key, {})
            val = src.get("current")
            if val is not None:
                snapshot[key] = round(float(val), 2)

        if not snapshot:
            return

        history[today] = snapshot

        # Keep only last N days (sorted desc, prune oldest)
        sorted_dates = sorted(history.keys(), reverse=True)[:MACRO_HISTORY_DAYS]
        pruned = {d: history[d] for d in sorted_dates}

        macro_path = str(MACRO_HISTORY_FILE)
        tmp_path = macro_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(pruned, f, indent=2, sort_keys=True)
        os.replace(tmp_path, macro_path)
    except Exception as e:
        log.warning(f"LUNA: error saving macro history — {e}")


def _load_macro_history() -> Dict[str, Dict[str, float]]:
    """Load macro_history.json. Returns {date: {indicator: value}}."""
    try:
        if MACRO_HISTORY_FILE.exists():
            data = json.loads(MACRO_HISTORY_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _build_macro_trends(macro: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build macro_trend dict from macro_history.json.
    Returns {indicator: {direction, 5d_change_pct, values}}.
    """
    history = _load_macro_history()
    if len(history) < 2:
        return {}

    sorted_dates = sorted(history.keys())  # oldest first
    trends = {}

    for key in ("dxy", "vix", "yields", "oil", "sp500", "gold"):
        values = []
        for d in sorted_dates:
            v = history[d].get(key)
            if v is not None:
                values.append(round(float(v), 2))

        if len(values) < 2:
            continue

        oldest = values[0]
        newest = values[-1]
        if oldest == 0:
            change_pct = 0.0
        else:
            change_pct = round(((newest - oldest) / abs(oldest)) * 100, 1)

        # Direction classification
        if abs(change_pct) < 0.5:
            direction = "FLAT"
        elif change_pct > 0:
            direction = "UP"
        else:
            direction = "DOWN"

        trends[key] = {
            "direction": direction,
            "5d_change_pct": change_pct,
            "values": values,
        }

    return trends


def _build_trend_context(trends: Dict[str, Any]) -> str:
    """Build text block for trend data to inject into Luna's data context."""
    if not trends:
        return ""

    labels = {
        "dxy": "DXY", "vix": "VIX", "yields": "Yields 10Y",
        "oil": "Oil WTI", "sp500": "S&P 500", "gold": "Gold",
    }
    arrows = {"UP": "▲", "DOWN": "▼", "FLAT": "▬"}
    lines = ["<macro_trend_5d>"]
    for key in ("dxy", "vix", "yields", "oil", "sp500", "gold"):
        t = trends.get(key)
        if not t:
            continue
        vals_str = " → ".join(str(v) for v in t["values"])
        arrow = arrows.get(t["direction"], "")
        lines.append(
            f"{labels.get(key, key)}: {vals_str} {arrow}{t['5d_change_pct']:+.1f}%"
        )
    lines.append("</macro_trend_5d>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Correlation Tracking (FLO-75)
# ---------------------------------------------------------------------------

def _calc_pearson(x: list, y: list) -> Optional[float]:
    """Calculate Pearson correlation coefficient for two lists of equal length."""
    n = len(x)
    if n < 3 or len(y) != n:
        return None
    mean_x = sum(x) / n
    mean_y = sum(y) / n
    num = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y))
    den_x = sum((xi - mean_x) ** 2 for xi in x) ** 0.5
    den_y = sum((yi - mean_y) ** 2 for yi in y) ** 0.5
    if den_x == 0 or den_y == 0:
        return None
    return round(num / (den_x * den_y), 2)


def _classify_correlation(value: float, normal_low: float, normal_high: float) -> str:
    """Classify correlation as NORMAL, WEAK, or BROKEN."""
    if normal_low <= value <= normal_high:
        return "NORMAL"
    # Check if sign flipped from expected
    if (normal_high < 0 and value > 0) or (normal_low > 0 and value < 0):
        return "BROKEN"
    return "WEAK"


def _build_correlations(macro: Dict[str, Any]) -> Dict[str, Any]:
    """
    Calculate 5-day rolling Pearson correlations: gold vs DXY, yields, S&P.
    Uses macro_history.json from FLO-74.
    """
    history = _load_macro_history()
    sorted_dates = sorted(history.keys())

    if len(sorted_dates) < 3:
        return {"status": "insufficient_data", "days": len(sorted_dates), "min_required": 3}

    # Extract series
    gold_vals, dxy_vals, yields_vals, sp500_vals = [], [], [], []
    for d in sorted_dates:
        entry = history[d]
        g = entry.get("gold")
        dx = entry.get("dxy")
        yd = entry.get("yields")
        sp = entry.get("sp500")
        if all(v is not None for v in [g, dx, yd, sp]):
            gold_vals.append(g)
            dxy_vals.append(dx)
            yields_vals.append(yd)
            sp500_vals.append(sp)

    if len(gold_vals) < 3:
        return {"status": "insufficient_data", "days": len(gold_vals), "min_required": 3}

    # Normal ranges (typical long-term correlations)
    pairs = {
        "gold_dxy": {"series": dxy_vals, "normal": (-0.9, -0.3)},
        "gold_yields": {"series": yields_vals, "normal": (-0.8, -0.2)},
        "gold_sp500": {"series": sp500_vals, "normal": (-0.5, 0.5)},
    }

    result = {"status": "ok", "days": len(gold_vals)}
    for name, cfg in pairs.items():
        corr = _calc_pearson(gold_vals, cfg["series"])
        if corr is None:
            result[name] = {"value": None, "status": "N/A"}
            continue
        status = _classify_correlation(corr, cfg["normal"][0], cfg["normal"][1])
        result[name] = {
            "value": corr,
            "status": status,
            "normal_range": list(cfg["normal"]),
        }

    return result


def _build_correlation_context(correlations: Dict[str, Any]) -> str:
    """Build text block for correlations to inject into Luna's data context."""
    if correlations.get("status") != "ok":
        days = correlations.get("days", 0)
        return f"<correlations>Insufficient data ({days} days, need 3+)</correlations>"

    labels = {
        "gold_dxy": "Gold-DXY",
        "gold_yields": "Gold-Yields",
        "gold_sp500": "Gold-S&P500",
    }
    lines = ["<correlations>"]
    for name in ("gold_dxy", "gold_yields", "gold_sp500"):
        c = correlations.get(name, {})
        val = c.get("value")
        status = c.get("status", "N/A")
        if val is not None:
            nr = c.get("normal_range", [])
            nr_str = f" (normal: {nr[0]} to {nr[1]})" if nr else ""
            lines.append(f"{labels.get(name, name)}: {val:+.2f} ({status}{nr_str})")
    lines.append("</correlations>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# MiMo AI Analysis (PRIMARY path)
# ---------------------------------------------------------------------------

def _analyze_with_mimo(macro: Dict[str, Any], echo_alerts: List[Dict],
                       calendar_events: List[Dict]) -> Optional[Dict[str, Any]]:
    """
    Call MiMo-V2-Flash with LUNA_SYSTEM_PROMPT + macro data context.
    Returns parsed JSON dict on success, None on failure.
    """
    if OpenAI is None:
        log.warning("LUNA: openai package not installed — cannot call MiMo")
        return None

    api_key = LUNA_API_KEY
    if not api_key:
        log.warning("LUNA: no LUNA_API_KEY configured — set env var or config")
        return None

    # Cost cap check
    cost_data = _load_daily_cost()
    if cost_data["total_usd"] >= LUNA_DAILY_COST_CAP:
        log.warning(
            f"LUNA: daily cost cap reached (${cost_data['total_usd']:.2f} >= "
            f"${LUNA_DAILY_COST_CAP:.2f})"
        )
        return None

    # Build user prompt (data context)
    data_context = _build_data_context(macro, echo_alerts, calendar_events)
    user_prompt = (
        "Analyze the current macro environment for gold trading. "
        "Return your analysis as the JSON structure specified in your instructions.\n\n"
        + data_context
    )

    t0 = time.time()
    try:
        client = OpenAI(api_key=api_key, base_url=LUNA_BASE_URL)
        response = client.chat.completions.create(
            model=LUNA_MODEL,
            messages=[
                {"role": "system", "content": LUNA_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
            max_completion_tokens=1024,
            response_format={"type": "json_object"},
            timeout=30,
        )

        elapsed_ms = int((time.time() - t0) * 1000)
        if not response.choices or not response.choices[0].message:
            log.error(f"LUNA: MiMo returned empty choices after {elapsed_ms}ms")
            return None
        raw = response.choices[0].message.content
        if not raw:
            log.error(f"LUNA: MiMo returned null content after {elapsed_ms}ms")
            return None
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            log.warning("LUNA: MiMo returned non-dict JSON")
            return None

        # Track cost
        usage = response.usage
        if usage:
            est = _estimate_cost(usage.prompt_tokens, usage.completion_tokens)
            cost_data["total_usd"] = round(cost_data["total_usd"] + est, 4)
            cost_data["calls"] += 1
            _save_daily_cost(cost_data)
            log.info(
                f"LUNA: MiMo response in {elapsed_ms}ms | "
                f"tokens: {usage.prompt_tokens}+{usage.completion_tokens} | "
                f"cost: ${est:.4f} (daily: ${cost_data['total_usd']:.4f})"
            )

        return parsed

    except json.JSONDecodeError as e:
        elapsed_ms = int((time.time() - t0) * 1000)
        log.warning(f"LUNA: MiMo returned invalid JSON ({elapsed_ms}ms) — {e} — retrying once...")
        # FLO-199: Single retry on JSON parse failure
        try:
            time.sleep(3)
            t1 = time.time()
            response2 = client.chat.completions.create(
                model=LUNA_MODEL,
                messages=[
                    {"role": "system", "content": LUNA_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.3,
                max_completion_tokens=1024,
                response_format={"type": "json_object"},
                timeout=30,
            )
            elapsed2 = int((time.time() - t1) * 1000)
            raw2 = response2.choices[0].message.content if response2.choices else ""
            if raw2:
                parsed2 = json.loads(raw2)
                if isinstance(parsed2, dict):
                    usage2 = response2.usage
                    if usage2:
                        est2 = _estimate_cost(usage2.prompt_tokens, usage2.completion_tokens)
                        cost_data["total_usd"] = round(cost_data["total_usd"] + est2, 4)
                        cost_data["calls"] += 1
                        _save_daily_cost(cost_data)
                    log.info(f"LUNA: MiMo retry succeeded ({elapsed2}ms)")
                    return parsed2
        except Exception as e2:
            log.warning(f"LUNA: MiMo retry also failed — {e2}")
        return None
    except Exception as e:
        elapsed_ms = int((time.time() - t0) * 1000)
        log.error(f"LUNA: MiMo API call failed ({elapsed_ms}ms) — {e}")
        return None


def _parse_mimo_response(parsed: Dict[str, Any], macro: Dict[str, Any]) -> LunaAnalysisResult:
    """Convert MiMo JSON response into LunaAnalysisResult."""
    # Ensure data_snapshot is present — inject from collected data if MiMo omits it
    data_snapshot = parsed.get("data_snapshot")
    if not data_snapshot or not isinstance(data_snapshot, dict):
        data_snapshot = _build_data_snapshot(macro)

    # P1-2: Validate environment
    environment = str(parsed.get("environment", "SAFE")).upper()
    if environment not in ("SAFE", "CAUTION", "DANGER"):
        log.warning(f"LUNA: invalid environment '{environment}' from MiMo — defaulting to SAFE")
        environment = "SAFE"

    # P1-2: Validate risk_level (int 1-10, default 3)
    try:
        risk_level = int(parsed.get("risk_level", 3))
        risk_level = max(1, min(10, risk_level))
    except (ValueError, TypeError):
        log.warning(f"LUNA: invalid risk_level '{parsed.get('risk_level')}' — defaulting to 3")
        risk_level = 3

    # P1-2: Validate directional_bias
    directional_bias = str(parsed.get("directional_bias", "NEUTRAL")).upper()
    if directional_bias not in ("BULLISH", "BEARISH", "NEUTRAL"):
        log.warning(f"LUNA: invalid directional_bias '{directional_bias}' — defaulting to NEUTRAL")
        directional_bias = "NEUTRAL"

    # P1-2: Validate bias_confidence (int 1-10, default 3)
    try:
        bias_confidence = int(parsed.get("bias_confidence", 3))
        bias_confidence = max(1, min(10, bias_confidence))
    except (ValueError, TypeError):
        log.warning(f"LUNA: invalid bias_confidence '{parsed.get('bias_confidence')}' — defaulting to 3")
        bias_confidence = 3

    return LunaAnalysisResult(
        timestamp=datetime.now(timezone.utc).isoformat(),
        environment=environment,
        risk_level=risk_level,
        directional_bias=directional_bias,
        bias_confidence=bias_confidence,
        key_factors=parsed.get("key_factors", []),
        patterns_detected=parsed.get("patterns_detected", []),
        pattern_details=parsed.get("pattern_details", {}),
        market_regime=parsed.get("market_regime", "mixed"),
        summary=parsed.get("summary", ""),
        next_events=parsed.get("next_events", []),
        data_snapshot=data_snapshot,
        macro_trend=_build_macro_trends(macro),
        correlations=_build_correlations(macro),
        source="mimo",
    )


# ---------------------------------------------------------------------------
# Local Deterministic Analysis (FALLBACK only)
# ---------------------------------------------------------------------------

def _detect_patterns(macro: Dict[str, Any], echo_alerts: List[Dict]) -> Dict[str, Any]:
    """Detect cross-asset patterns from macro data. Returns {detected: [], details: {}}."""
    detected: List[str] = []
    details: Dict[str, str] = {}

    vix = macro.get("vix", {})
    dxy = macro.get("dxy", {})
    oil = macro.get("oil", {})
    sp500 = macro.get("sp500", {})
    gold = macro.get("gold", {})

    vix_val = vix.get("current")
    vix_chg = vix.get("change_percent", 0) or 0
    dxy_chg = dxy.get("change_percent", 0) or 0
    oil_chg = oil.get("change_percent", 0) or 0
    sp500_chg = sp500.get("change_percent", 0) or 0
    gold_chg = gold.get("change_percent", 0) or 0

    # forced_liquidation
    if (vix_val is not None and vix_val > 25 and vix_chg > 0
            and oil_chg > 3 and gold_chg < 0 and sp500_chg < 0):
        detected.append("forced_liquidation")
        details["forced_liquidation"] = (
            f"VIX {vix_val:.1f} (rising {vix_chg:+.1f}%), Oil {oil_chg:+.1f}%, "
            f"Gold {gold_chg:+.2f}% (falling), S&P {sp500_chg:+.2f}% (falling). "
            "Institutions selling everything for cash."
        )

    # safe_haven_flow
    if vix_chg > 0 and dxy_chg < 0 and gold_chg > 0:
        detected.append("safe_haven_flow")
        details["safe_haven_flow"] = (
            f"VIX rising ({vix_chg:+.1f}%), DXY falling ({dxy_chg:+.2f}%), "
            f"Gold rising ({gold_chg:+.2f}%). Classic flight to safety."
        )

    # news_price_divergence
    critical_important = [a for a in echo_alerts
                          if a.get("classification") in ("CRITICAL", "IMPORTANT")]
    if critical_important:
        bullish_news = sum(1 for a in critical_important if a.get("gold_impact") == "BULLISH")
        bearish_news = sum(1 for a in critical_important if a.get("gold_impact") == "BEARISH")
        if bullish_news > bearish_news and gold_chg < -0.1:
            detected.append("news_price_divergence")
            details["news_price_divergence"] = (
                f"{bullish_news} BULLISH Echo alerts but gold is {gold_chg:+.2f}%. "
                "Market not reacting as expected — divergence may resolve violently."
            )
        elif bearish_news > bullish_news and gold_chg > 0.1:
            detected.append("news_price_divergence")
            details["news_price_divergence"] = (
                f"{bearish_news} BEARISH Echo alerts but gold is {gold_chg:+.2f}%. "
                "Market not reacting as expected — divergence may resolve violently."
            )

    # dollar_gold_correlation_break
    if dxy_chg != 0 and gold_chg != 0:
        same_direction = (dxy_chg > 0 and gold_chg > 0) or (dxy_chg < 0 and gold_chg < 0)
        if same_direction:
            detected.append("dollar_gold_correlation_break")
            direction = "both positive" if dxy_chg > 0 else "both negative"
            details["dollar_gold_correlation_break"] = (
                f"DXY {dxy_chg:+.2f}% and Gold {gold_chg:+.2f}% moving in same direction ({direction}). "
                "Normal inverse correlation broken — unusual driver at work."
            )

    return {"detected": detected, "details": details}


def _classify_environment(macro: Dict[str, Any], echo_alerts: List[Dict],
                          patterns: List[str]) -> str:
    """Classify environment as SAFE / CAUTION / DANGER."""
    vix = macro.get("vix", {})
    dxy = macro.get("dxy", {})
    oil = macro.get("oil", {})
    yields = macro.get("yields", {})

    vix_val = vix.get("current")
    dxy_chg = abs(dxy.get("change_percent", 0) or 0)
    oil_chg = abs(oil.get("change_percent", 0) or 0)
    yields_chg = abs(yields.get("change_percent", 0) or 0)

    has_critical = any(a.get("classification") == "CRITICAL" for a in echo_alerts)
    has_important_gold = any(
        a.get("classification") == "IMPORTANT" and a.get("gold_impact") != "NEUTRAL"
        for a in echo_alerts
    )

    if vix_val is not None and vix_val > 30:
        return "DANGER"
    if has_critical:
        return "DANGER"
    if patterns and any(p in ("forced_liquidation", "safe_haven_flow") for p in patterns):
        return "DANGER"

    caution_count = 0
    if vix_val is not None and 20 <= vix_val <= 30:
        caution_count += 1
    if dxy_chg > 1:
        caution_count += 1
    if oil_chg > 3:
        caution_count += 1
    if has_important_gold:
        caution_count += 1
    if yields_chg > 2:
        caution_count += 1

    if caution_count >= 3:
        return "DANGER"
    if caution_count >= 1:
        return "CAUTION"

    return "SAFE"


def _compute_risk_level(environment: str, macro: Dict[str, Any],
                        patterns: List[str]) -> int:
    """Compute risk_level 1-10 consistent with environment classification."""
    if environment == "SAFE":
        base = 2
    elif environment == "CAUTION":
        base = 5
    else:
        base = 8

    vix_val = (macro.get("vix", {}).get("current") or 0)
    dxy_chg = abs(macro.get("dxy", {}).get("change_percent", 0) or 0)

    adjustment = 0
    if vix_val > 35:
        adjustment += 1
    if dxy_chg > 2:
        adjustment += 1
    if len(patterns) >= 2:
        adjustment += 1

    level = base + adjustment
    if environment == "SAFE":
        return min(max(level, 1), 3)
    elif environment == "CAUTION":
        return min(max(level, 4), 6)
    else:
        return min(max(level, 7), 10)


def _run_local_analysis(macro: Dict[str, Any], echo_alerts: List[Dict],
                        calendar_events: List[Dict]) -> LunaAnalysisResult:
    """Produce a Luna analysis using deterministic rules (FALLBACK — no AI)."""
    now = datetime.now(timezone.utc).isoformat()

    pattern_result = _detect_patterns(macro, echo_alerts)
    patterns = pattern_result["detected"]
    pattern_details = pattern_result["details"]

    environment = _classify_environment(macro, echo_alerts, patterns)
    risk_level = _compute_risk_level(environment, macro, patterns)

    # Directional bias from macro signals
    bias_signals = 0
    null_count = 0

    dxy_chg = macro.get("dxy", {}).get("change_percent")
    if dxy_chg is not None:
        if dxy_chg < -0.3:
            bias_signals += 1
        elif dxy_chg > 0.3:
            bias_signals -= 1
    else:
        null_count += 1

    vix_chg = macro.get("vix", {}).get("change_percent")
    if vix_chg is not None:
        if vix_chg > 1:
            bias_signals += 1
        elif vix_chg < -1:
            bias_signals -= 1
    else:
        null_count += 1

    gold_chg = macro.get("gold", {}).get("change_percent")
    if gold_chg is not None:
        if gold_chg > 0.2:
            bias_signals += 1
        elif gold_chg < -0.2:
            bias_signals -= 1
    else:
        null_count += 1

    bullish_alerts = sum(1 for a in echo_alerts
                         if a.get("classification") in ("CRITICAL", "IMPORTANT")
                         and a.get("gold_impact") == "BULLISH")
    bearish_alerts = sum(1 for a in echo_alerts
                         if a.get("classification") in ("CRITICAL", "IMPORTANT")
                         and a.get("gold_impact") == "BEARISH")
    if bullish_alerts > bearish_alerts:
        bias_signals += 1
    elif bearish_alerts > bullish_alerts:
        bias_signals -= 1

    if bias_signals >= 2:
        directional_bias = "BULLISH"
    elif bias_signals <= -2:
        directional_bias = "BEARISH"
    else:
        directional_bias = "NEUTRAL"

    aligned = abs(bias_signals)
    if null_count > 0:
        bias_confidence = min(5, 2 + aligned)
    elif aligned >= 3:
        bias_confidence = 8
    elif aligned >= 2:
        bias_confidence = 6
    elif aligned >= 1:
        bias_confidence = 4
    else:
        bias_confidence = 2

    vix_val = macro.get("vix", {}).get("current")
    sp500_chg = macro.get("sp500", {}).get("change_percent", 0) or 0
    if vix_val is not None and vix_val > 35:
        market_regime = "crisis"
    elif "forced_liquidation" in patterns:
        market_regime = "crisis"
    elif vix_val is not None and vix_val > 25:
        market_regime = "risk_off"
    elif sp500_chg > 0.5 and (vix_val is None or vix_val < 18):
        market_regime = "risk_on"
    else:
        market_regime = "mixed"

    key_factors = []
    dxy = macro.get("dxy", {})
    vix = macro.get("vix", {})
    oil = macro.get("oil", {})
    gold = macro.get("gold", {})

    if dxy.get("current") is not None:
        key_factors.append(f"DXY at {dxy['current']} ({dxy.get('change_percent', 0):+.2f}%)")
    if vix.get("current") is not None:
        key_factors.append(f"VIX at {vix['current']:.1f} ({vix.get('change_percent', 0):+.1f}%)")
    if oil.get("current") is not None:
        key_factors.append(f"Oil at ${oil['current']} ({oil.get('change_percent', 0):+.1f}%)")
    if gold.get("current") is not None:
        key_factors.append(f"Gold at ${gold['current']} ({gold.get('change_percent', 0):+.2f}%)")

    critical_count = sum(1 for a in echo_alerts if a.get("classification") == "CRITICAL")
    if critical_count > 0:
        key_factors.append(f"{critical_count} CRITICAL Echo alert(s) active")

    summary_parts = [f"Environment: {environment}."]
    if patterns:
        summary_parts.append(f"Patterns: {', '.join(patterns)}.")
    if directional_bias != "NEUTRAL":
        summary_parts.append(f"Macro bias {directional_bias} (confidence {bias_confidence}/10).")
    else:
        summary_parts.append("No clear directional bias from macro data.")
    summary = " ".join(summary_parts)

    next_events = []
    for ev in calendar_events[:3]:
        next_events.append({
            "event": ev.get("event", "Unknown"),
            "time": ev.get("time", ""),
            "impact": ev.get("impact", "MEDIUM"),
        })

    data_snapshot = _build_data_snapshot(macro)

    return LunaAnalysisResult(
        timestamp=now,
        environment=environment,
        risk_level=risk_level,
        directional_bias=directional_bias,
        bias_confidence=bias_confidence,
        key_factors=key_factors,
        patterns_detected=patterns,
        pattern_details=pattern_details,
        market_regime=market_regime,
        summary=summary,
        next_events=next_events,
        data_snapshot=data_snapshot,
        macro_trend=_build_macro_trends(macro),
        correlations=_build_correlations(macro),
        source="local_fallback",
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _save_brief(result: LunaAnalysisResult) -> None:
    """Save Luna analysis result to data/luna_brief.json (atomic write)."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        brief_path = str(BRIEF_FILE)
        tmp_path = brief_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(asdict(result), f, indent=2, ensure_ascii=False, default=str)
        os.replace(tmp_path, brief_path)
    except Exception as e:
        log.warning(f"LUNA: error saving brief — {e}")


def load_luna_brief() -> Optional[Dict[str, Any]]:
    """Load the latest Luna brief from disk. Returns None if unavailable or stale (>30 min)."""
    try:
        if not BRIEF_FILE.exists():
            return None
        data = json.loads(BRIEF_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        # P0-1: TTL check — brief older than 30 min is stale
        ts = data.get("timestamp")
        if ts:
            try:
                brief_time = datetime.fromisoformat(ts)
                if brief_time.tzinfo is None:
                    brief_time = brief_time.replace(tzinfo=timezone.utc)
                age = datetime.now(timezone.utc) - brief_time
                if age.total_seconds() > 1800:
                    log.warning("LUNA | Brief stale (>30 min) — returning None")
                    return None
            except (ValueError, TypeError):
                pass  # unparseable timestamp — let it through
        return data
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Main Entry Point
# ---------------------------------------------------------------------------

def run_luna_analysis() -> LunaAnalysisResult:
    """
    Run Luna macro analysis.

    PRIMARY: MiMo-V2-Flash AI interpretation
    FALLBACK: deterministic local analysis if MiMo unavailable
    """
    log.info("LUNA: starting macro analysis...")

    try:
        # 1. Collect all macro data
        macro = _get_macro_data()
        echo_alerts = _get_echo_alerts()
        calendar_events = _get_calendar_events()

        # 2. PRIMARY — MiMo AI analysis
        mimo_response = _analyze_with_mimo(macro, echo_alerts, calendar_events)

        if mimo_response is not None:
            result = _parse_mimo_response(mimo_response, macro)
            log.info(
                f"LUNA: MiMo analysis — {result.environment} | "
                f"risk {result.risk_level}/10 | bias {result.directional_bias} "
                f"({result.bias_confidence}/10) | patterns: {result.patterns_detected or 'none'}"
            )
        else:
            # 3. FALLBACK — deterministic local analysis
            log.warning("LUNA: AI unavailable — using local fallback")
            result = _run_local_analysis(macro, echo_alerts, calendar_events)
            log.info(
                f"LUNA: local fallback — {result.environment} | "
                f"risk {result.risk_level}/10 | bias {result.directional_bias} "
                f"({result.bias_confidence}/10)"
            )

        # FLO-87: Recovery detection heuristic — post-process Luna's bias
        try:
            import config as _rcfg
            _recovery_threshold = float(getattr(_rcfg, "LUNA_RECOVERY_THRESHOLD_PCT", 50))
            _gold_current = macro.get("gold", {}).get("current")
            if _gold_current and result.directional_bias == "BEARISH":
                import MetaTrader5 as _mt5r
                _gi = _mt5r.symbol_info("XAUUSD")
                if _gi:
                    _day_high = getattr(_gi, "session_high", 0) or 0
                    _day_low = getattr(_gi, "session_low", 0) or 0
                    _day_range = _day_high - _day_low
                    if _day_range > 0 and _day_low > 0:
                        _recovery_pct = ((_gold_current - _day_low) / _day_range) * 100
                        if _recovery_pct >= _recovery_threshold:
                            log.info(
                                f"LUNA | Recovery detected: price recovered {_recovery_pct:.0f}% from daily low "
                                f"${_day_low:.2f} to ${_gold_current:.2f} (high ${_day_high:.2f}) "
                                f"— shifting bias BEARISH → NEUTRAL"
                            )
                            result = LunaAnalysisResult(
                                timestamp=result.timestamp,
                                environment=result.environment,
                                risk_level=result.risk_level,
                                directional_bias="NEUTRAL",
                                bias_confidence=max(1, result.bias_confidence - 2),
                                key_factors=result.key_factors + [f"Recovery override: {_recovery_pct:.0f}% from daily low"],
                                patterns_detected=result.patterns_detected,
                                pattern_details=result.pattern_details,
                                market_regime=result.market_regime,
                                summary=result.summary + f" [Recovery override: BEARISH→NEUTRAL, {_recovery_pct:.0f}% from low]",
                                data_snapshot=result.data_snapshot,
                                source=result.source,
                                correlations=result.correlations,
                            )
        except Exception as e:
            log.debug(f"LUNA | recovery check error (ignored): {e}")

        # 4. Save macro history snapshot (one per day)
        _save_macro_snapshot(macro)

        # 5. Save to luna_brief.json
        _save_brief(result)

        # 5. Record agent event for Trade Room feed
        try:
            from db_writer import record_agent_event
            record_agent_event(
                event_type="LUNA_ANALYSIS",
                content=result.summary,
                payload=asdict(result),
                author="LUNA",
            )
        except Exception as e:
            log.warning(f"LUNA: error recording event — {e}")

        # 6. Discord card for DANGER/CAUTION (FLO-78)
        if result.environment in ("DANGER", "CAUTION"):
            try:
                from discord_cards import build_luna_brief_card, send_built_card
                # Get echo aggregate for sentiment field
                agg = _get_echo_aggregate()
                sentiment = None
                if agg:
                    w4h = agg.get("4h", {})
                    if w4h.get("total", 0) > 0:
                        sentiment = f"{w4h.get('dominant', 'N/A')} ({int(w4h.get('bullish', 0) / w4h['total'] * 100)}% bullish, {w4h['total']} headlines)"

                card = build_luna_brief_card(
                    environment=result.environment,
                    risk=result.risk_level,
                    bias=result.directional_bias,
                    regime=result.market_regime,
                    patterns=result.patterns_detected or None,
                    summary=result.summary,
                    macro_data=result.data_snapshot,
                    sentiment=sentiment,
                )
                send_built_card(card)
            except Exception:
                pass

        return result

    except Exception as e:
        log.error(f"LUNA: analysis failed — {e}")
        return LunaAnalysisResult(
            timestamp=datetime.now(timezone.utc).isoformat(),
            environment="SAFE",
            risk_level=3,
            directional_bias="NEUTRAL",
            bias_confidence=1,
            summary=f"Luna analysis failed: {e}",
            source="error",
            error=str(e),
        )


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------

def get_luna_prompt() -> str:
    """Return the Luna system prompt."""
    return LUNA_SYSTEM_PROMPT.strip()


def get_data_context() -> str:
    """Fetch live macro data and return formatted context string for AI use."""
    macro = _get_macro_data()
    echo_alerts = _get_echo_alerts()
    calendar_events = _get_calendar_events()
    return _build_data_context(macro, echo_alerts, calendar_events)


# ---------------------------------------------------------------------------
# CLI Test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    print("=" * 60)
    print("LUNA MACRO ANALYST — Test Run")
    print("=" * 60)

    result = run_luna_analysis()

    if result.error:
        print(f"\nERROR: {result.error}")
        sys.exit(1)

    print(f"\nSource:       {result.source}")
    print(f"Environment:  {result.environment}")
    print(f"Risk Level:   {result.risk_level}/10")
    print(f"Bias:         {result.directional_bias} (confidence {result.bias_confidence}/10)")
    print(f"Regime:       {result.market_regime}")
    print(f"Patterns:     {result.patterns_detected or 'none'}")
    print(f"\nKey Factors:")
    for f in result.key_factors:
        print(f"  - {f}")
    print(f"\nSummary: {result.summary}")
    print(f"\nData Snapshot:")
    print(json.dumps(result.data_snapshot, indent=2))
    print(f"\nSaved to: {BRIEF_FILE}")
