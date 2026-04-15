"""
Discord Rich Embed Card System (FLO-78)

Standardized embed cards for all FlokiWatch agents.
Every Discord message uses embeds — no plain text.
"""

import os
import requests
from datetime import datetime, timezone
from tz_utils import utc_iso  # FLO-309
from typing import Any, Dict, List, Optional

from logger import log


# ---------------------------------------------------------------------------
# Agent Colors (decimal for Discord API)
# ---------------------------------------------------------------------------

COLORS = {
    "floki": 11206400,    # #AAFF00 chartreuse
    "rex": 16738101,      # #FF6B35 orange-red
    "luna": 14680315,     # #E040FB magenta
    "echo": 58879,        # #00E5FF cyan
    "simba": 3900150,     # #3B82F6 blue
    "sage": 16757504,     # #FFB300 amber
    "system": 7042944,    # #6B7280 grey
    "profit": 5763719,    # #57F287 green (Discord green)
    "loss": 15548997,     # #ED4245 red (Discord red)
    "alert": 16711680,    # #FF0000 pure red
}


# ---------------------------------------------------------------------------
# Webhook Map: channel key -> env var name
# ---------------------------------------------------------------------------

WEBHOOK_MAP = {
    "floki": "DISCORD_WEBHOOK_FLOKI",
    "rex": "DISCORD_WEBHOOK_REX",
    "luna": "DISCORD_WEBHOOK_LUNA",
    "echo": "DISCORD_WEBHOOK_ECHO",
    "simba": "DISCORD_WEBHOOK_SIMBA",
    "sage": "DISCORD_WEBHOOK_SAGE",
    "daily": "DISCORD_WEBHOOK_DAILY",
    "weekly": "DISCORD_WEBHOOK_WEEKLY",
    "monthly": "DISCORD_WEBHOOK_MONTHLY",
    "backtests": "DISCORD_WEBHOOK_BACKTESTS",
    "errors": "DISCORD_WEBHOOK_ERRORS",
    "changelog": "DISCORD_WEBHOOK_CHANGELOG",
    "announcements": "DISCORD_WEBHOOK_ANNOUNCEMENTS",
}

# Fallback: old env var names for backwards compat
_FALLBACK_MAP = {
    "floki": "DISCORD_WEBHOOK_SIGNALS",
    "rex": "DISCORD_WEBHOOK_BRAIN",
    "luna": "DISCORD_WEBHOOK_DASHBOARD",
    "echo": "DISCORD_WEBHOOK_STATUS",
    "simba": "DISCORD_WEBHOOK_TRADES",
}


def _get_webhook_url(channel: str) -> str:
    """Get webhook URL for a channel, with fallback."""
    env_key = WEBHOOK_MAP.get(channel, "")
    url = os.environ.get(env_key, "") if env_key else ""
    if not url:
        fallback_key = _FALLBACK_MAP.get(channel, "")
        if fallback_key:
            url = os.environ.get(fallback_key, "")
    if not url:
        url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    return url


# ---------------------------------------------------------------------------
# Core send function
# ---------------------------------------------------------------------------

def _utc_footer() -> str:
    return f"FlokiWatch \u2022 {datetime.now(timezone.utc).strftime('%H:%M')} UTC"


def send_card(
    channel: str,
    color: int,
    author_name: str,
    title: str,
    fields: Optional[List[Dict[str, Any]]] = None,
    description: Optional[str] = None,
    footer: Optional[str] = None,
) -> bool:
    """
    Send a standardized Discord embed card.
    Returns True on success, False on failure. Never raises.
    """
    url = _get_webhook_url(channel)
    if not url:
        log.debug(f"[DISCORD_CARDS] No webhook for channel={channel} | {title}")
        return False

    try:
        embed: Dict[str, Any] = {
            "color": color,
            "author": {"name": author_name},
            "title": title[:256],
            "footer": {"text": footer or _utc_footer()},
            "timestamp": utc_iso(),  # FLO-309
        }
        if description:
            embed["description"] = description[:4096]
        if fields:
            embed["fields"] = fields[:25]  # Discord limit

        payload = {
            "username": "FlokiWatch",
            "embeds": [embed],
        }

        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code in (200, 204):
            return True
        log.warning(f"[DISCORD_CARDS] {channel} status={resp.status_code}")
        return False
    except Exception as e:
        log.warning(f"[DISCORD_CARDS] {channel} error: {e}")
        return False


def _f(name: str, value: Any, inline: bool = True) -> Dict[str, Any]:
    """Shorthand field builder."""
    return {"name": str(name), "value": str(value), "inline": inline}


# ---------------------------------------------------------------------------
# Card Builders
# ---------------------------------------------------------------------------

def build_floki_open_card(
    direction: str, price: float, confidence: float,
    sl: float, tp: float, rex_verdict: Optional[str] = None,
    luna_env: Optional[str] = None, luna_risk: Optional[int] = None,
    session: Optional[str] = None,
) -> Dict[str, Any]:
    emoji = "\U0001F7E2" if direction == "BUY" else "\U0001F534"
    return {
        "channel": "floki",
        "color": COLORS["floki"],
        "author_name": "\U0001F415 FLOKI \u2014 TRADE SIGNAL",
        "title": f"{emoji} {direction} XAU/USD @ ${price:,.2f}",
        "fields": [
            _f("Confidence", f"{confidence:.0f}%"),
            _f("SL", f"${sl:,.2f}"),
            _f("TP", f"${tp:,.2f}"),
            _f("Rex", f"{rex_verdict or 'N/A'} \U0001F4CA"),
            _f("Luna", f"{luna_env or 'N/A'} risk {luna_risk or '?'}/10"),
            _f("Session", session or "N/A"),
        ],
    }


def build_floki_close_card(
    ticket: int, direction: str, pnl: float, pips: Optional[float] = None,
    duration: Optional[str] = None, entry: Optional[float] = None,
    exit_price: Optional[float] = None, close_reason: Optional[str] = None,
    day_pnl: Optional[float] = None,
) -> Dict[str, Any]:
    is_profit = pnl >= 0
    pips_str = f" ({pips:+.0f} pips)" if pips is not None else ""
    return {
        "channel": "floki",
        "color": COLORS["profit"] if is_profit else COLORS["loss"],
        "author_name": "\U0001F415 FLOKI \u2014 TRADE CLOSED",
        "title": f"{'WIN' if is_profit else 'LOSS'} #{ticket} \u2014 P&L ${pnl:+.2f}{pips_str}",
        "fields": [
            _f("Direction", direction),
            _f("Duration", duration or "N/A"),
            _f("Reason", close_reason or "N/A"),
            _f("Entry", f"${entry:,.2f}" if entry else "N/A"),
            _f("Exit", f"${exit_price:,.2f}" if exit_price else "N/A"),
            _f("Day P&L", f"${day_pnl:+.2f}" if day_pnl is not None else "N/A"),
        ],
    }


def build_rex_debate_card(
    floki_wants: str, rex_says: str, agree: bool,
    data_verified: bool = False, suggestion: Optional[str] = None,
) -> Dict[str, Any]:
    verdict = "AGREE" if agree else "DISAGREE"
    dv = " \U0001F4CA VERIFIED" if data_verified else ""
    fields = [
        _f("Floki wants", floki_wants),
        _f("Rex says", rex_says[:200]),
    ]
    if suggestion:
        fields.append(_f("Suggestion", suggestion, inline=False))
    return {
        "channel": "rex",
        "color": COLORS["rex"],
        "author_name": f"\U0001F98E REX \u2014 DEBATE{dv}",
        "title": f"{verdict} \u2014 {rex_says[:80]}",
        "fields": fields,
    }


def build_luna_brief_card(
    environment: str, risk: int, bias: str, regime: str,
    patterns: Optional[List[str]] = None, summary: str = "",
    macro_data: Optional[Dict[str, Any]] = None,
    sentiment: Optional[str] = None,
) -> Dict[str, Any]:
    fields = []
    if macro_data:
        md = macro_data
        for key, label in [("dxy", "DXY"), ("vix", "VIX"), ("yields_10y", "10Y"),
                           ("oil", "Oil"), ("sp500", "S&P"), ("gold", "Gold")]:
            d = md.get(key, {})
            val = d.get("value") or d.get("price")
            chg = d.get("change_pct", 0)
            if val is not None:
                prefix = "$" if key in ("oil", "gold") else ""
                suffix = "%" if key == "yields_10y" else ""
                fields.append(_f(label, f"{prefix}{val}{suffix} ({chg:+.2f}%)"))

    if patterns:
        fields.append(_f("Patterns", ", ".join(patterns), inline=False))
    if summary:
        fields.append(_f("Summary", summary[:500], inline=False))
    if sentiment:
        fields.append(_f("Sentiment", sentiment))

    env_color = COLORS["alert"] if environment == "DANGER" else (COLORS["sage"] if environment == "CAUTION" else COLORS["luna"])
    return {
        "channel": "luna",
        "color": env_color,
        "author_name": "\U0001F43A LUNA \u2014 MACRO BRIEF",
        "title": f"{environment} \u2014 Risk {risk}/10 | {bias} | {regime.upper()}",
        "fields": fields,
        "footer": f"FlokiWatch \u2022 via MiMo AI \u2022 {datetime.now(timezone.utc).strftime('%H:%M')} UTC",
    }


def build_echo_critical_card(
    headline: str, headline_count: int = 1,
    sources: Optional[List[str]] = None, gold_impact: str = "NEUTRAL",
    age_hours: float = 0, sentiment: Optional[str] = None,
    summary: str = "",
) -> Dict[str, Any]:
    count_str = f" ({headline_count} sources)" if headline_count > 1 else ""
    fields = [
        _f("Gold Impact", gold_impact),
        _f("Sources", ", ".join(sources[:5]) if sources else "N/A"),
        _f("Age", f"{age_hours:.1f}h"),
    ]
    if sentiment:
        fields.append(_f("Sentiment (1h)", sentiment))
    if summary:
        fields.append(_f("Summary", summary[:500], inline=False))
    return {
        "channel": "echo",
        "color": COLORS["echo"],
        "author_name": "\U0001F987 ECHO \u2014 CRITICAL ALERT",
        "title": f"{headline[:200]}{count_str}",
        "fields": fields,
    }


def build_simba_wake_card(
    condition_type: str, threshold: Any = None, current: Any = None,
    velocity: Optional[str] = None, group_info: Optional[str] = None,
) -> Dict[str, Any]:
    fields = [
        _f("Type", condition_type),
        _f("Threshold", threshold or "N/A"),
        _f("Current", current or "N/A"),
    ]
    if velocity:
        fields.append(_f("Velocity", velocity))
    if group_info:
        fields.append(_f("Group", group_info))
    fields.append(_f("Action", "Floki called"))
    return {
        "channel": "simba",
        "color": COLORS["simba"],
        "author_name": "\U0001F415 SIMBA \u2014 WAKE TRIGGER",
        "title": f"Condition MET \u2014 {condition_type}",
        "fields": fields,
    }


def build_sage_daily_card(
    trades: int, wins: int, losses: int, pnl: float,
    best: Optional[float] = None, worst: Optional[float] = None,
    profit_factor: Optional[float] = None,
    recommendations: Optional[List[str]] = None,
) -> Dict[str, Any]:
    wr = int(wins / (wins + losses) * 100) if (wins + losses) > 0 else 0
    fields = [
        _f("Wins/Losses", f"{wins}W / {losses}L"),
        _f("Best", f"${best:+.2f}" if best is not None else "N/A"),
        _f("Worst", f"${worst:+.2f}" if worst is not None else "N/A"),
    ]
    if profit_factor is not None:
        fields.append(_f("Profit Factor", f"{profit_factor:.1f}"))
    if recommendations:
        recs_text = "\n".join(f"\u2022 {r}" for r in recommendations[:3])
        fields.append(_f("Recommendations", recs_text, inline=False))
    return {
        "channel": "daily",
        "color": COLORS["sage"],
        "author_name": "\U0001F989 SAGE \u2014 DAILY REPORT",
        "title": f"{trades} trades | {wr}% WR | P&L ${pnl:+.2f}",
        "fields": fields,
    }


def build_sage_weekly_card(
    this_week: Dict[str, Any], last_week: Dict[str, Any],
    comparison: Dict[str, Any],
) -> Dict[str, Any]:
    tw = this_week
    lw = last_week
    trend = comparison.get("trend", "STABLE")
    arrow = "\u25B2" if trend == "IMPROVING" else ("\u25BC" if trend == "DECLINING" else "\u2594")
    tw_wr = int(tw.get("win_rate", 0) * 100)
    lw_wr = int(lw.get("win_rate", 0) * 100)
    wr_chg = comparison.get("win_rate_change")
    wr_chg_str = f" ({wr_chg:+.0%})" if wr_chg is not None else ""

    fields = [
        _f("This week", f"{tw.get('trades', 0)} trades, {tw_wr}% WR, ${tw.get('pnl', 0):+.2f}"),
        _f("Last week", f"{lw.get('trades', 0)} trades, {lw_wr}% WR, ${lw.get('pnl', 0):+.2f}"),
        _f("WR change", f"{wr_chg_str.strip()}" if wr_chg is not None else "N/A"),
        _f("P&L change", f"${comparison.get('pnl_change', 0):+.2f}" if comparison.get("pnl_change") is not None else "N/A"),
    ]

    # Session breakdown
    sess = tw.get("by_session", {})
    parts = []
    for s in ("ny", "london", "asian"):
        sv = sess.get(s)
        if sv and sv.get("trades", 0) > 0:
            parts.append(f"{s.capitalize()} {int(sv.get('win_rate', 0) * 100)}%")
    if parts:
        fields.append(_f("Sessions", " | ".join(parts), inline=False))

    return {
        "channel": "weekly",
        "color": COLORS["sage"],
        "author_name": "\U0001F989 SAGE \u2014 WEEKLY REPORT",
        "title": f"{trend} {arrow} \u2014 {tw.get('trades', 0)} trades, {tw_wr}% WR, ${tw.get('pnl', 0):+.2f}",
        "fields": fields,
        "footer": f"FlokiWatch \u2022 Friday {datetime.now(timezone.utc).strftime('%H:%M')} UTC",
    }


def build_sage_alert_card(
    daily_pnl: float, streak: int, trades_today: int,
    wins: int = 0, losses: int = 0,
) -> Dict[str, Any]:
    return {
        "channel": "sage",
        "color": COLORS["alert"],
        "author_name": "\U0001F989 SAGE \u2014 \u26A0\uFE0F DRAWDOWN ALERT",
        "title": f"Daily P&L ${daily_pnl:+.2f} | {streak} consecutive losses",
        "fields": [
            _f("Daily P&L", f"${daily_pnl:+.2f}"),
            _f("Streak", f"{streak} losses"),
            _f("Trades today", f"{trades_today} ({wins}W/{losses}L)"),
            _f("Action", "Session memory updated, Floki cautioned", inline=False),
        ],
    }


def build_system_error_card(
    component: str, error: str, impact: str = "",
) -> Dict[str, Any]:
    fields = [
        _f("Component", component),
        _f("Error", str(error)[:200]),
    ]
    if impact:
        fields.append(_f("Impact", impact))
    return {
        "channel": "errors",
        "color": COLORS["system"],
        "author_name": "\u2699\uFE0F SYSTEM \u2014 ERROR",
        "title": str(error)[:200],
        "fields": fields,
    }


# ---------------------------------------------------------------------------
# Convenience: send a built card
# ---------------------------------------------------------------------------

def send_built_card(card: Dict[str, Any]) -> bool:
    """Send a card dict returned by any build_* function."""
    return send_card(
        channel=card.get("channel", "errors"),
        color=card.get("color", COLORS["system"]),
        author_name=card.get("author_name", "FlokiWatch"),
        title=card.get("title", ""),
        fields=card.get("fields"),
        description=card.get("description"),
        footer=card.get("footer"),
    )


# ---------------------------------------------------------------------------
# Test: send one [TEST] card to every configured channel
# ---------------------------------------------------------------------------

def send_test_cards() -> None:
    """Send one [TEST] card to every configured webhook channel."""
    print("Sending test cards to all configured webhooks...\n")

    cards = [
        build_floki_open_card("BUY", 4574.90, 75, 4560.00, 4600.00,
                              rex_verdict="AGREE", luna_env="DANGER", luna_risk=9, session="NY"),
        build_floki_close_card(12345, "SELL", 18.50, pips=15, duration="2h 15m",
                               entry=4574.90, exit_price=4556.40, close_reason="TP hit", day_pnl=42.50),
        build_rex_debate_card("OPEN_BUY 75% conf", "RSI at 34 is not oversold",
                              agree=False, data_verified=True, suggestion="Wait for RSI < 30"),
        build_luna_brief_card("DANGER", 9, "BULLISH", "crisis",
                              patterns=["safe_haven_flow"], summary="Extreme geopolitical stress.",
                              sentiment="83% BULLISH (4h)"),
        build_echo_critical_card("Iran fires missiles toward US-UK base", headline_count=5,
                                 sources=["Bloomberg", "Reuters", "Sky News"], gold_impact="BULLISH",
                                 age_hours=0.3, sentiment="83% BULLISH", summary="Escalating Middle East tensions."),
        build_simba_wake_card("rsi_above", threshold=70, current=71.3,
                              velocity="RAPID", group_info="A (2/2 AND met)"),
        build_sage_daily_card(5, 4, 1, 22.50, best=12.00, worst=-3.50, profit_factor=2.8,
                              recommendations=["NY session strongest", "Consider tighter SL on Asian trades"]),
        build_sage_weekly_card(
            {"trades": 15, "win_rate": 0.68, "pnl": 67.99, "by_session": {"ny": {"trades": 3, "win_rate": 0.67}, "london": {"trades": 5, "win_rate": 0.80}, "asian": {"trades": 10, "win_rate": 0.60}}},
            {"trades": 27, "win_rate": 0.52, "pnl": -59.65, "by_session": {}},
            {"win_rate_change": 0.16, "pnl_change": 127.64, "trend": "IMPROVING"},
        ),
        build_sage_alert_card(-35.00, 4, 5, wins=1, losses=4),
        build_system_error_card("ai_agent.py", "Gemini API timeout (60s)", "Floki cycle skipped"),
    ]

    for card in cards:
        # Prefix title with [TEST]
        card["title"] = f"[TEST] {card.get('title', '')}"
        channel = card.get("channel", "errors")
        url = _get_webhook_url(channel)
        status = "configured" if url else "NO WEBHOOK"
        result = send_built_card(card)
        emoji = "\u2705" if result else "\u274C"
        print(f"  {emoji} {channel:<12} | {card.get('author_name', '')[:30]:<30} | {'sent' if result else status}")

    print("\nDone.")
