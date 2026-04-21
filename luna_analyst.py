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
from tz_utils import utc_iso  # FLO-309
from pathlib import Path
from typing import Any, Dict, List, Optional

from logger import log
from tz_utils import trading_day_utc  # FLO-286

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

LUNA_SYSTEM_PROMPT = """You are Luna, a senior macro analyst at a gold-focused commodity desk. Your job is to report what the macro data shows — specific numbers, specific changes, specific correlations, specific patterns — so Floki, the portfolio manager, can read the data and form his own view.

You are NOT a trader. You do NOT categorize conditions as cautious, dangerous, or safe. You do NOT rate risk on a 1-10 scale. You do NOT assign directional bias (bullish, bearish, neutral). You do NOT issue confidence scores. You do NOT label the overall market regime. You do NOT write summaries that interpret what the data means for the trade.

Floki decides whether conditions are cautious, dangerous, or safe. Floki decides direction. Floki decides confidence. Your job is to give him clean numbers and observable facts.

Words that MUST NOT appear anywhere in your output: "cautious", "caution", "dangerous", "danger", "risky", "risk on", "risk off", "safe", "crisis", "volatile", "hesitate", "careful", "trust", "stable environment", "bullish environment", "bearish environment", "mixed regime", "risk-on", "risk-off". Do not describe the environment with an emotional or directional label. Report numbers.

DATA YOU RECEIVE:
- DXY (US Dollar Index): value + 24h change
- VIX (Fear Index): value + 24h change
- Treasury Yields 10Y: value + 24h change
- Oil (Crude WTI): price + 24h change + 1h change
- S&P 500: value + 24h change
- Gold price: current price + 24h change + 1h change + day high/low + 3-day high/low + distance from 3-day peak
- GLD ETF: price + volume + 24h change (volume is a conviction signal, not a direction)
- USD/CNY: exchange rate + 24h change
- Real Yields (TIPS 10Y, FRED DFII10): current value + change
- Fed Funds Rate (FRED FEDFUNDS): current value + change
- Breakeven Inflation 10Y (FRED T10YIE): current value + change
- CPI All Urban (FRED CPIAUCSL): current value + change
- Echo classified news alerts: each has classification (CRITICAL / IMPORTANT / ROUTINE), gold_impact label, and a summary. Treat the classification as factual metadata, not as a directive.
- Economic calendar: upcoming events with impact level and time

PATTERN DETECTION — flag these patterns when ALL criteria are met. Report only the pattern name in the "patterns_detected" list. Do NOT add interpretation, direction, or recommended action.

forced_liquidation:
  VIX > 25 AND rising + Oil change > +3% in 24h + Gold change negative + S&P 500 change negative.

safe_haven_flow:
  VIX rising (change positive) + DXY falling (change negative) + Gold rising (change positive).

news_price_divergence:
  Echo CRITICAL/IMPORTANT alerts dominantly in one direction (majority BULLISH or majority BEARISH gold_impact) while gold 24h change is in the opposite direction.

dollar_gold_correlation_break:
  DXY change_24h and Gold change_24h are BOTH positive or BOTH negative (same direction; the typical relationship is inverse).

blow_off_reversal:
  Gold is down > 1.5% from the 3-day high AND DXY is falling AND yields are falling (macro indicators would typically favor gold, but price is selling off).

A downstream Python reconciler validates each pattern you claim against the actual data and drops unverified claims. Claim only patterns you can justify from the numbers provided. Pattern names above are terms of art for specific market-microstructure conditions — use them as labels only, do not add prose interpretation.

OUTPUT FORMAT — Return ONLY valid JSON. Exact schema:
{
  "timestamp": "ISO 8601 UTC with Z suffix",
  "data_snapshot": {
    "dxy":         {"value": X, "change_pct_24h": Y, "trend_3d": "rising"|"falling"|"flat"},
    "vix":         {"value": X, "change_pct_24h": Y, "trend_3d": "rising"|"falling"|"flat"},
    "yields_10y":  {"value": X, "change_pct_24h": Y, "trend_3d": "rising"|"falling"|"flat"},
    "oil_wti":     {"value": X, "change_pct_24h": Y},
    "sp500":       {"value": X, "change_pct_24h": Y},
    "gold":        {"value": X, "change_pct_24h": Y, "dist_from_3d_high_pct": Z, "3d_high": W},
    "gld_volume":  {"avg_5d_vs_baseline": X, "rising_price": bool, "status": "accumulation"|"distribution"|"quiet_bid"|"quiet"},
    "usdcny":      {"value": X, "change_pct_24h": Y},
    "real_yields": {"value": X, "change": Y},
    "fed_funds":   {"value": X, "change": Y},
    "breakeven":   {"value": X, "change": Y},
    "cpi":         {"value": X, "change": Y}
  },
  "correlations": {
    "gold_dxy":    {"current": X, "typical": Y},
    "gold_silver": {"current": X, "typical": Y},
    "gold_10y":    {"current": X, "typical": Y}
  },
  "patterns_detected": ["pattern_name", "..."],
  "key_factors": ["3-5 short factual statements, each referencing a specific number"],
  "next_events": [{"event": "name", "time": "HH:MM UTC", "impact": "HIGH"|"MEDIUM"|"LOW"}]
}

KEY_FACTORS RULES:
- 3 to 5 statements maximum.
- Each statement is a specific observation anchored to a number from the data. Example OK: "Gold -1.98% from 3-day high 4891.62." Example NOT OK: "Gold looks weak."
- Every claim must reference a number. No floating adjectives.
- Use the forbidden-words list above as a self-audit before returning.
- If a data source is null, you may note that ("DXY data unavailable this cycle") but do not invent values.

DATA NOTES:
- "Real Yield proxy (live)" is computed from real-time ^TNX minus breakeven inflation. Prefer it for intraday observation. FRED DFII10 is end-of-day and can be hours stale.
- GLD "status" values are volume-based conviction descriptors, not flow labels. "accumulation" = high volume + rising price. "distribution" = high volume + falling price. "quiet_bid" = low volume + rising price. "quiet" = low volume + no clear direction.
- Correlations: report raw numeric "current" and "typical" values only. Do NOT add status labels like "broken" or "weak" — Floki compares the numbers himself.
- Trends: "rising" / "falling" / "flat" are observational 3-day directional descriptors, not sentiment labels. Use them; do not add emotional qualifiers.
- Pattern names (forced_liquidation, safe_haven_flow, news_price_divergence, dollar_gold_correlation_break, blow_off_reversal) are the only valid entries in "patterns_detected". Any other string will be dropped by the reconciler."""


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
    # FLO-298 fix 1: pattern_details is {name: {text, first_seen, age_minutes}}
    # on disk. Typed as Dict[str, Any] to accommodate both forms during transition
    # (producer code emits strings; _save_brief enriches to nested dict before write).
    pattern_details: Dict[str, Any] = field(default_factory=dict)
    market_regime: str = "mixed"
    summary: str = ""
    next_events: List[Dict[str, str]] = field(default_factory=list)
    data_snapshot: Dict[str, Any] = field(default_factory=dict)
    macro_trend: Dict[str, Any] = field(default_factory=dict)
    correlations: Dict[str, Any] = field(default_factory=dict)
    source: str = "mimo"      # "mimo" or "local_fallback"
    error: Optional[str] = None
    headlines_consumed: List[Dict[str, str]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Cost Tracker
# ---------------------------------------------------------------------------

def _load_daily_cost() -> Dict:
    try:
        if COST_FILE.exists():
            data = json.loads(COST_FILE.read_text(encoding="utf-8"))
            if data.get("date") == trading_day_utc():
                return data
    except Exception:
        pass
    return {"date": trading_day_utc(), "total_usd": 0.0, "calls": 0}


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
            # Day high/low from symbol_info
            if _gi:
                try:
                    gold["day_high"] = round(float(_gi.bidhigh), 2) if _gi.bidhigh else None
                    gold["day_low"] = round(float(_gi.bidlow), 2) if _gi.bidlow else None
                except Exception:
                    pass
            # 3-day high/low from D1 candles
            try:
                _d1 = _mt5.copy_rates_from_pos("XAUUSD", _mt5.TIMEFRAME_D1, 0, 4)
                if _d1 is not None and len(_d1) >= 3:
                    _d3h = max(float(r["high"]) for r in _d1[-3:])
                    _d3l = min(float(r["low"]) for r in _d1[-3:])
                    gold["3d_high"] = round(_d3h, 2)
                    gold["3d_low"] = round(_d3l, 2)
                    gold["from_3d_high_pct"] = round((_gt.bid - _d3h) / _d3h * 100, 2)
                    gold["from_3d_low_pct"] = round((_gt.bid - _d3l) / _d3l * 100, 2)
            except Exception:
                pass
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
    _gold_line = f"Gold: ${gold.get('current', 'N/A')} (24h: {gold.get('change_percent', 'N/A')}%, 1h: {gold.get('change_1h_percent', 'N/A')}%)"
    lines.append(_gold_line)
    # Day and 3-day range context for blow-off/crash detection
    _dh = gold.get("day_high")
    _dl = gold.get("day_low")
    if _dh and _dl:
        lines.append(f"  Day range: ${_dl} - ${_dh}")
    _3h = gold.get("3d_high")
    _3l = gold.get("3d_low")
    if _3h and _3l:
        _from_h = gold.get("from_3d_high_pct", "?")
        _from_l = gold.get("from_3d_low_pct", "?")
        _cur = gold.get("current", 0)
        _dist_h = round(_cur - _3h, 0) if _cur and _3h else "?"
        _dist_l = round(_cur - _3l, 0) if _cur and _3l else "?"
        lines.append(f"  3-day range: ${_3l} - ${_3h} | From 3d-high: {_dist_h} ({_from_h}%) | From 3d-low: +{_dist_l} (+{_from_l}%)")

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

    # FLO-232: Price regime context so Luna can weigh macro vs price action
    try:
        import os as _os_lr
        _bs_path_lr = _os_lr.path.join(_os_lr.path.dirname(_os_lr.path.abspath(__file__)), "data", "bot_state.json")
        if _os_lr.path.exists(_bs_path_lr):
            with open(_bs_path_lr, "r", encoding="utf-8") as _bsf_lr:
                _bs_lr = json.load(_bsf_lr)
            _regime_lr = _bs_lr.get("market_regime", {})
            _regime_name = _regime_lr.get("regime", "unknown")
            _regime_conf = _regime_lr.get("confidence", "")
            _regime_dur = _regime_lr.get("duration", "")
            lines.append(f"\n<price_regime>")
            lines.append(f"XAU/USD price regime: {_regime_name} (confidence: {_regime_conf}, duration: {_regime_dur})")
            lines.append(f"</price_regime>")
    except Exception:
        pass

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

    # FLO-236: Deep Research insights from Google Search
    try:
        from deep_search import load_deep_research
        dr = load_deep_research()
        if dr:
            lines.append("\n<analyst_research>")
            lines.append(f"Analyst consensus: {dr.get('analyst_consensus', 'unknown')}")
            _ki = dr.get("key_insight", "")
            if _ki:
                lines.append(f"Key insight: {_ki}")
            _tgt = dr.get("price_targets", {})
            if _tgt:
                _sup = ", ".join(str(s) for s in (_tgt.get("support") or []))
                _res = ", ".join(str(r) for r in (_tgt.get("resistance") or []))
                if _sup:
                    lines.append(f"Analyst support levels: {_sup}")
                if _res:
                    lines.append(f"Analyst resistance levels: {_res}")
            _risks = dr.get("risks_this_week", [])
            if _risks:
                lines.append(f"Risks this week: {'; '.join(str(r) for r in _risks[:5])}")
            _src = dr.get("sources", [])
            if _src:
                lines.append(f"Sources: {', '.join(str(s) for s in _src[:5])}")
            lines.append("</analyst_research>")
    except Exception:
        pass

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
        today = trading_day_utc()
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
    """Bug G follow-up: gutted to no-op. Previously emitted NORMAL / WEAK / BROKEN
    labels that prescribed interpretation on top of raw numbers, violating Escola 1.
    Floki now compares the raw current vs typical values himself. Retained as a
    callable stub so any out-of-tree caller doesn't AttributeError.
    """
    return ""


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

    # Bug G follow-up: remove per-pair "status" prescriptive labels (NORMAL /
    # WEAK / BROKEN). Emit raw current + typical (normal_range) values only;
    # Floki compares and decides whether a correlation is broken. Top-level
    # "status" key ('ok' / 'insufficient_data') is retained — it's a
    # data-quality flag consumed by the dashboard, not a prescription.
    result = {"status": "ok", "days": len(gold_vals)}
    for name, cfg in pairs.items():
        corr = _calc_pearson(gold_vals, cfg["series"])
        if corr is None:
            result[name] = {"value": None, "normal_range": list(cfg["normal"])}
            continue
        result[name] = {
            "value": corr,
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
    # Bug G follow-up: strip per-pair status label from the prompt context.
    # Luna now sees raw current + typical numbers only.
    lines = ["<correlations>"]
    for name in ("gold_dxy", "gold_yields", "gold_sp500"):
        c = correlations.get(name, {})
        val = c.get("value")
        if val is not None:
            nr = c.get("normal_range", [])
            nr_str = f" (typical: {nr[0]} to {nr[1]})" if nr else ""
            lines.append(f"{labels.get(name, name)}: {val:+.2f}{nr_str}")
    lines.append("</correlations>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# MiMo AI Analysis (PRIMARY path)
# ---------------------------------------------------------------------------

def _build_luna_user_prompt(macro: Dict[str, Any], echo_alerts: List[Dict],
                            calendar_events: List[Dict]) -> str:
    """Shared user prompt builder for both MiMo and Gemini paths."""
    data_context = _build_data_context(macro, echo_alerts, calendar_events)
    return (
        "Analyze the current macro environment for gold trading. "
        "Return your analysis as the JSON structure specified in your instructions.\n\n"
        + data_context
    )


def _analyze_with_gemini(macro: Dict[str, Any], echo_alerts: List[Dict],
                         calendar_events: List[Dict]) -> Optional[Dict[str, Any]]:
    """FLO-294: Gemini Flash secondary path. Same prompt + JSON contract as MiMo.

    FLO-322: raised max_output_tokens from 1024 → 4096. Gemini 3 Flash is a
    reasoning model that spends "thinking" tokens before output; 1024 was
    being fully consumed by 900-1700 thinking tokens, leaving the actual
    response truncated at ~40 chars (finish_reason=MAX_TOKENS). Live test
    with 1024 produced 39 output chars and failed parse; 4096 produced 578
    and parsed all 12 expected Luna keys cleanly. Echo worked because it
    uses the helper's default 2048 with a much smaller system prompt.
    """
    from mimo_fallback import call_gemini_json
    user_prompt = _build_luna_user_prompt(macro, echo_alerts, calendar_events)
    parsed = call_gemini_json(
        system=LUNA_SYSTEM_PROMPT,
        user_text=user_prompt,
        agent="luna",
        max_output_tokens=4096,
    )
    if parsed is None or not isinstance(parsed, dict):
        return None
    return parsed


def _analyze_with_mimo(macro: Dict[str, Any], echo_alerts: List[Dict],
                       calendar_events: List[Dict]) -> Optional[Dict[str, Any]]:
    """Primary AI path: MiMo via OpenAI SDK.
    Returns parsed dict on success, None on failure.
    FLO-294: detects 451 errors and sets MiMo cooldown.
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
        # FLO-294: La Liga IP block (HTTP 451) → set cooldown so subsequent
        # cycles skip MiMo and use Gemini Flash directly.
        from mimo_fallback import is_451_error, set_cooldown
        if is_451_error(e):
            set_cooldown("luna", reason="MiMo returned 451 (La Liga IP block)")
        log.error(f"LUNA: MiMo API call failed ({elapsed_ms}ms) — {e}")
        return None


def _parse_mimo_response(
    parsed: Dict[str, Any],
    macro: Dict[str, Any],
    echo_alerts: Optional[List[Dict[str, Any]]] = None,
) -> LunaAnalysisResult:
    """Convert MiMo JSON response into LunaAnalysisResult.

    Bug A fix: patterns_detected from the LLM is reconciled against the
    deterministic _detect_patterns() output (intersection). LLM-only
    patterns are dropped with a WARNING log line so Floki never sees
    hallucinated patterns contradicted by the correlations block.
    echo_alerts is optional for backward-compat; when omitted, the
    news_price_divergence detector path reads an empty list."""
    # Ensure data_snapshot is present — inject from collected data if MiMo omits it
    data_snapshot = parsed.get("data_snapshot")
    if not data_snapshot or not isinstance(data_snapshot, dict):
        data_snapshot = _build_data_snapshot(macro)

    # Bug G: Luna prescriptive fields (environment, risk_level, directional_bias,
    # bias_confidence, market_regime, summary) were removed from schema.
    # Legacy dataclass fields remain with empty defaults for backward compat;
    # _save_brief pops them before JSON write (Test A/B on disk shape).
    # If MiMo still emits legacy keys despite the updated prompt, log a warning
    # so regression is visible — then discard.
    _legacy_keys = ("environment", "risk_level", "directional_bias", "bias_confidence", "market_regime", "summary")
    _leaked = [k for k in _legacy_keys if k in parsed]
    if _leaked:
        log.warning(f"LUNA: MiMo response still included legacy keys {_leaked} — discarded (Bug G)")

    # Bug A commit 2: Reconcile LLM-reported patterns with deterministic detector.
    # LLM sometimes hallucinates patterns (e.g. claims "both negative directionally"
    # when DXY is positive). Intersect LLM output with _detect_patterns() so only
    # patterns with verifiable numeric evidence survive. _detect_patterns() MUST
    # cover every prompt pattern (docstring warning there).
    llm_patterns = parsed.get("patterns_detected", []) or []
    llm_details = parsed.get("pattern_details", {}) or {}
    if not isinstance(llm_patterns, list):
        llm_patterns = []
    if not isinstance(llm_details, dict):
        llm_details = {}

    try:
        det = _detect_patterns(macro, echo_alerts or [])
        verified = set(det.get("detected", []))
        det_details = det.get("details", {}) or {}

        final_patterns = [p for p in llm_patterns if p in verified]
        dropped = [p for p in llm_patterns if p not in verified]
        if dropped:
            log.warning(
                f"LUNA: dropped unverified LLM pattern(s): {dropped} "
                f"(verified by _detect_patterns: {sorted(verified)})"
            )
        # Prefer LLM's explanation text where present (richer prose);
        # fall back to the deterministic detector's text if LLM text missing.
        final_details = {
            p: (llm_details.get(p) or det_details.get(p) or "")
            for p in final_patterns
        }
    except Exception as e:
        # Defensive fallback: if _detect_patterns crashes, preserve current
        # behavior (pass LLM output through) rather than drop everything.
        # A visible WARNING surfaces the issue without degrading service.
        log.warning(
            f"LUNA: _detect_patterns raised {type(e).__name__}: {e}. "
            f"Falling back to LLM patterns unfiltered: {llm_patterns}"
        )
        final_patterns = list(llm_patterns)
        final_details = dict(llm_details)

    return LunaAnalysisResult(
        timestamp=utc_iso(),  # FLO-309: Z suffix per Rule 22
        # Bug G: legacy prescriptive fields emptied; _save_brief pops them pre-write.
        environment="",
        risk_level=0,
        directional_bias="",
        bias_confidence=0,
        key_factors=parsed.get("key_factors", []),
        patterns_detected=final_patterns,
        pattern_details=final_details,
        market_regime="",
        summary="",
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
    """Detect cross-asset patterns from macro data. Returns {detected: [], details: {}}.

    WARNING — MUST be kept in sync with Luna's LLM prompt PATTERN DETECTION section
    (SYSTEM_PROMPT around line 85). Adding a pattern description to the prompt
    without adding a corresponding branch here will cause the new pattern to be
    silently dropped by _parse_mimo_response() reconciliation (intersection of
    LLM output with this deterministic detector).

    Currently covered patterns (5/5):
      - forced_liquidation
      - safe_haven_flow
      - news_price_divergence
      - dollar_gold_correlation_break
      - blow_off_reversal

    Last synced: 2026-04-20 (Bug A Commit 1 added blow_off_reversal detector,
    Commit 2 wired reconciliation into _parse_mimo_response via intersection).
    If you add a pattern to the prompt, add a branch here AND update this date.
    """
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

    # blow_off_reversal
    # Prompt criteria (SYSTEM_PROMPT lines ~112-117):
    #   Gold down >1.5% from 3-day high WHILE DXY falling AND yields falling.
    # Data field: gold["from_3d_high_pct"] populated at line ~332 on MT5 path
    # (current − 3d_high) / 3d_high * 100 → negative when below peak.
    # Field is absent on Yahoo fallback path; guard with `is not None`.
    yields = macro.get("yields", {})
    yields_chg = yields.get("change_percent", 0) or 0
    gold_from_3d_high_pct = gold.get("from_3d_high_pct")
    if (gold_from_3d_high_pct is not None
            and gold_from_3d_high_pct <= -1.5
            and dxy_chg < 0
            and yields_chg < 0):
        detected.append("blow_off_reversal")
        details["blow_off_reversal"] = (
            f"Gold {gold_from_3d_high_pct:+.2f}% from 3-day high while DXY "
            f"{dxy_chg:+.2f}% and yields {yields_chg:+.2f}% (both falling = bullish macro). "
            "Price diverging from fundamentals — forced liquidation or exhaustion top."
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
    """Produce a Luna analysis using deterministic rules (FALLBACK — no AI).

    Bug G: produces only observational fields. No environment classification,
    no risk_level, no directional_bias, no bias_confidence, no market_regime,
    no interpretive summary. key_factors are numeric observations only.
    """
    now = utc_iso()  # FLO-309

    pattern_result = _detect_patterns(macro, echo_alerts)
    patterns = pattern_result["detected"]
    pattern_details = pattern_result["details"]

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
        # Bug G: legacy prescriptive fields emptied; _save_brief pops them pre-write.
        environment="",
        risk_level=0,
        directional_bias="",
        bias_confidence=0,
        key_factors=key_factors,
        patterns_detected=patterns,
        pattern_details=pattern_details,
        market_regime="",
        summary="",
        next_events=next_events,
        data_snapshot=data_snapshot,
        macro_trend=_build_macro_trends(macro),
        correlations=_build_correlations(macro),
        source="local_fallback",
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _enrich_pattern_details_with_age(result: LunaAnalysisResult) -> None:
    """FLO-298 fix 1: Enrich pattern_details with first_seen + age_minutes.

    Reads the previous brief to carry forward first_seen for patterns that
    persist; sets first_seen = now for new patterns. Converts the
    Dict[str, str] shape produced by _detect_patterns into
    Dict[str, {text, first_seen, age_minutes}] in place on the result.
    """
    try:
        # Load previous brief's pattern_details if available
        prev_details: Dict[str, Any] = {}
        try:
            if BRIEF_FILE.exists():
                prev = json.loads(BRIEF_FILE.read_text(encoding="utf-8"))
                if isinstance(prev, dict):
                    prev_details = prev.get("pattern_details") or {}
        except Exception:
            prev_details = {}

        now_dt = datetime.now(timezone.utc)

        enriched: Dict[str, Dict[str, Any]] = {}
        for name in result.patterns_detected:
            raw = result.pattern_details.get(name)
            # Accept either legacy string or already-enriched dict from producer
            if isinstance(raw, dict):
                text = str(raw.get("text") or "")
            else:
                text = str(raw or "")

            # Carry forward first_seen if this pattern was in the previous brief
            first_seen_iso = None
            prev_entry = prev_details.get(name)
            if isinstance(prev_entry, dict):
                first_seen_iso = prev_entry.get("first_seen")
            # If prev was a legacy string, we don't know when — treat as fresh
            if not first_seen_iso:
                first_seen_iso = now_dt.isoformat().replace("+00:00", "Z")

            # Compute age in minutes
            try:
                fs = datetime.fromisoformat(first_seen_iso.replace("Z", "+00:00"))
                if fs.tzinfo is None:
                    fs = fs.replace(tzinfo=timezone.utc)
                age_minutes = max(0, int((now_dt - fs).total_seconds() // 60))
            except Exception:
                age_minutes = 0

            enriched[name] = {
                "text": text,
                "first_seen": first_seen_iso,
                "age_minutes": age_minutes,
            }

        result.pattern_details = enriched
    except Exception as e:
        # Visibility: failures here silently leave pattern_details as raw strings,
        # which breaks downstream {text, first_seen, age_minutes} consumers.
        log.warning(f"LUNA: pattern age enrichment failed (non-blocking): {e}")
        # Guarantee dict-shape fallback so consumers don't KeyError.
        try:
            _now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            _fallback: Dict[str, Dict[str, Any]] = {}
            for _name in getattr(result, "patterns_detected", []) or []:
                _raw = (result.pattern_details or {}).get(_name)
                if isinstance(_raw, dict):
                    _fallback[_name] = _raw
                else:
                    _fallback[_name] = {"text": str(_raw or ""), "first_seen": _now_iso, "age_minutes": 0}
            result.pattern_details = _fallback
        except Exception:
            pass


def _save_brief(result: LunaAnalysisResult) -> None:
    """Save Luna analysis result to data/luna_brief.json (atomic write).

    Bug G: strip legacy prescriptive fields from disk payload. Dataclass
    keeps them for backward-compat in-memory API, but JSON output matches
    the new observational-only schema.
    """
    try:
        _enrich_pattern_details_with_age(result)  # FLO-298 fix 1
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        brief_path = str(BRIEF_FILE)
        tmp_path = brief_path + ".tmp"
        payload = asdict(result)
        # Bug G: strip legacy prescriptive fields — no longer reach Floki or dashboard.
        for _lk in ("environment", "risk_level", "directional_bias", "bias_confidence", "market_regime", "summary"):
            payload.pop(_lk, None)
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False, default=str)
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

        # FLO-294: 3-tier AI path — MiMo (primary) → Gemini Flash (secondary) → local (last resort)
        from mimo_fallback import is_in_cooldown, clear_cooldown_if_set
        ai_response = None
        ai_source = None
        if not is_in_cooldown("luna"):
            ai_response = _analyze_with_mimo(macro, echo_alerts, calendar_events)
            if ai_response is not None:
                clear_cooldown_if_set("luna")
                ai_source = "mimo"
        else:
            log.info("LUNA: MiMo in cooldown — going straight to Gemini Flash")
        if ai_response is None:
            ai_response = _analyze_with_gemini(macro, echo_alerts, calendar_events)
            if ai_response is not None:
                ai_source = "gemini_fallback"

        if ai_response is not None:
            result = _parse_mimo_response(ai_response, macro, echo_alerts)
            # Tag source so dashboard can show which tier produced this analysis
            try:
                result.source = ai_source or "ai"
            except Exception:
                pass
            log.info(
                f"LUNA: {ai_source} analysis — patterns: {result.patterns_detected or 'none'} | "
                f"key_factors: {len(result.key_factors)}"
            )
        else:
            # 3. LAST RESORT — deterministic local analysis (both AI tiers down)
            log.warning("LUNA: MiMo + Gemini both unavailable — using local fallback")
            result = _run_local_analysis(macro, echo_alerts, calendar_events)
            log.info(
                f"LUNA: local fallback — patterns: {result.patterns_detected or 'none'} | "
                f"key_factors: {len(result.key_factors)}"
            )

        # Bug G: FLO-87 recovery block (BEARISH→NEUTRAL override) removed —
        # directional_bias field no longer exists in the new schema, Floki
        # forms his own directional view from the data.

        # 4. Save macro history snapshot (one per day)
        _save_macro_snapshot(macro)

        # FLO-238: Attach headlines Luna consumed (for Trade Room transparency)
        try:
            _hc = []
            for _ea in echo_alerts[:10]:
                if isinstance(_ea, dict):
                    _title = _ea.get("title") or _ea.get("headline") or _ea.get("representative_headline") or ""
                    if _title:
                        _hc.append({
                            "title": str(_title)[:120],
                            "severity": _ea.get("classification", _ea.get("severity", "ROUTINE")),
                        })
            result.headlines_consumed = _hc
        except Exception:
            pass

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

        # Bug G: Discord DANGER/CAUTION card removed — prescriptive labels
        # no longer produced. If a neutral Luna Discord card is needed, add
        # a data-forward card in follow-up commit.

        return result

    except Exception as e:
        log.error(f"LUNA: analysis failed — {e}")
        return LunaAnalysisResult(
            timestamp=utc_iso(),  # FLO-309: Z suffix per Rule 22
            # Bug G: legacy prescriptive fields emptied.
            environment="",
            risk_level=0,
            directional_bias="",
            bias_confidence=0,
            market_regime="",
            summary="",
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
    print(f"Patterns:     {result.patterns_detected or 'none'}")
    print(f"\nKey Factors:")
    for f in result.key_factors:
        print(f"  - {f}")
    print(f"\nData Snapshot:")
    print(json.dumps(result.data_snapshot, indent=2))
    print(f"\nCorrelations:")
    print(json.dumps(result.correlations, indent=2))
    print(f"\nSaved to: {BRIEF_FILE}")
