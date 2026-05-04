"""
ALERTS - Discord Alert System
Sends notifications to Discord via Webhook
"""

import os
import requests
from datetime import datetime, timedelta
from tz_utils import utc_now  # FLO-286
from typing import Dict, List, Optional

import config
from logger import log


CHANNEL_SIGNALS = "signals"
CHANNEL_DASHBOARD = "dashboard"
CHANNEL_BRAIN = "brain"
CHANNEL_TRADES = "trades"
CHANNEL_STATUS = "status"
CHANNEL_DAILY = "daily"
CHANNEL_WEEKLY = "weekly"
CHANNEL_MONTHLY = "monthly"
CHANNEL_BACKTEST = "backtest"
CHANNEL_ERRORS = "errors"
CHANNEL_CHANGELOG = "changelog"
CHANNEL_DATA_NEEDS = "data_needs"  # FLO-302: Floki's structured self-assessment → Hermano

WEBHOOK_ENV_KEYS = {
    CHANNEL_SIGNALS: "DISCORD_WEBHOOK_SIGNALS",
    CHANNEL_DASHBOARD: "DISCORD_WEBHOOK_DASHBOARD",
    CHANNEL_BRAIN: "DISCORD_WEBHOOK_BRAIN",
    CHANNEL_TRADES: "DISCORD_WEBHOOK_TRADES",
    CHANNEL_STATUS: "DISCORD_WEBHOOK_STATUS",
    CHANNEL_DAILY: "DISCORD_WEBHOOK_DAILY",
    CHANNEL_WEEKLY: "DISCORD_WEBHOOK_WEEKLY",
    CHANNEL_MONTHLY: "DISCORD_WEBHOOK_MONTHLY",
    CHANNEL_BACKTEST: "DISCORD_WEBHOOK_BACKTEST",
    CHANNEL_ERRORS: "DISCORD_WEBHOOK_ERRORS",
    CHANNEL_CHANGELOG: "DISCORD_WEBHOOK_CHANGELOG",
    CHANNEL_DATA_NEEDS: "DISCORD_WEBHOOK_DATA_NEEDS",  # FLO-302
}

ERROR_RATE_LIMIT_SECONDS = 60
_error_last_sent: Dict[str, datetime] = {}

# EA Bridge state tracking for offline alerts
_ea_bridge_last_online: bool = True  # Assume online at start
_ea_bridge_alert_sent: bool = False  # Prevent duplicate alerts


def _get_config_value(name: str, default: str = "") -> str:
    value = getattr(config, name, default)
    return value or ""


def _format_price(value: Optional[float]) -> str:
    if value is None:
        return "N/A"
    return f"${value:,.2f}"


def _format_pips(value: Optional[float]) -> str:
    if value is None:
        return "N/A"
    return f"{value:+.1f} pips"


def _extract_scenario(brain_summary: str) -> Optional[str]:
    if not brain_summary:
        return None
    for line in brain_summary.splitlines():
        if line.lower().startswith("scenario:"):
            return line.split(":", 1)[1].strip()
    return None


def _utc_timestamp() -> str:
    return utc_now().strftime("%Y-%m-%d %H:%M UTC")


def _rate_limited(key: str, cooldown_seconds: int = None) -> bool:
    now = datetime.utcnow()
    limit = cooldown_seconds if cooldown_seconds is not None else ERROR_RATE_LIMIT_SECONDS
    last_sent = _error_last_sent.get(key)
    if last_sent and (now - last_sent) < timedelta(seconds=limit):
        return True
    _error_last_sent[key] = now
    return False


class DiscordAlert:
    """Discord alert manager (single webhook)."""

    def __init__(self, webhook_url: str = None, bot_name: Optional[str] = None):
        self.webhook_url = webhook_url or ""
        self.bot_name = bot_name or config.DISCORD_BOT_NAME
        self.enabled = bool(self.webhook_url)

    def _post(self, payload: Dict) -> bool:
        if not self.enabled:
            log.debug("[DISCORD DISABLED] webhook not configured")
            return False
        try:
            response = requests.post(
                self.webhook_url,
                json=payload,
                timeout=10
            )

            if response.status_code in [200, 204]:
                return True
            log.warning(f"[DISCORD ERROR] Status: {response.status_code}")
            return False
        except Exception as e:
            log.warning(f"[DISCORD ERROR] {e}")
            return False

    def send(
        self,
        message: str,
        alert_type: str = "info",
        title: Optional[str] = None
    ) -> bool:
        """Send alert to Discord (plain text)."""
        if not self.enabled:
            log.debug(f"[DISCORD DISABLED] {message}")
            return False

        emojis = {
            "info": "ℹ️",
            "success": "✅",
            "warning": "⚠️",
            "error": "❌",
            "trade": "📈",
            "profit": "💰",
            "loss": "🔴",
            "alert": "🔔",
        }

        emoji = emojis.get(alert_type, "ℹ️")
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if title:
            full_message = f"{emoji} **{title}**\n{message}\n`{timestamp}`"
        else:
            full_message = f"{emoji} {message}\n`{timestamp}`"

        if len(full_message) > 1990:
            full_message = full_message[:1990] + "..."

        payload = {
            "content": full_message,
            "username": self.bot_name,
        }

        return self._post(payload)

    def send_embed(
        self,
        title: str,
        description: str,
        color: int = 0x00FF00,
        fields: Optional[List[Dict]] = None,
        footer_text: Optional[str] = None,
    ) -> bool:
        """Send formatted embed message."""
        if not self.enabled:
            log.debug(f"[DISCORD DISABLED] {title}: {description}")
            return False

        if len(description) > 4090:
            description = description[:4090] + "..."

        embed = {
            "title": title,
            "description": description,
            "color": color,
            "timestamp": datetime.utcnow().isoformat(),
            "footer": {"text": footer_text or self.bot_name},
        }

        if fields:
            embed["fields"] = fields

        payload = {
            "username": self.bot_name,
            "embeds": [embed],
        }

        return self._post(payload)


class DiscordAlertRouter:
    """Routes Discord alerts to channel-specific webhooks."""

    def __init__(self, webhook_map: Optional[Dict[str, str]] = None, bot_name: Optional[str] = None):
        self.bot_name = bot_name or config.DISCORD_BOT_NAME
        default_webhook = _get_config_value("DISCORD_WEBHOOK_URL")
        if webhook_map is None:
            webhook_map = {
                channel: _get_config_value(env_key) or default_webhook
                for channel, env_key in WEBHOOK_ENV_KEYS.items()
            }
        self.clients = {
            channel: DiscordAlert(url, self.bot_name)
            for channel, url in webhook_map.items()
        }

    def _client_for(self, channel: str) -> Optional[DiscordAlert]:
        client = self.clients.get(channel)
        if client and client.enabled:
            return client
        return None

    def send(self, channel: str, message: str, alert_type: str = "info", title: Optional[str] = None) -> bool:
        client = self._client_for(channel)
        if not client:
            log.debug(f"[DISCORD DISABLED] Channel={channel} | {message}")
            return False
        return client.send(message, alert_type=alert_type, title=title)

    def send_embed(
        self,
        channel: str,
        title: str,
        description: str,
        color: int = 0x00FF00,
        fields: Optional[List[Dict]] = None,
        footer_text: Optional[str] = None,
    ) -> bool:
        client = self._client_for(channel)
        if not client:
            log.debug(f"[DISCORD DISABLED] Channel={channel} | {title}: {description}")
            return False
        return client.send_embed(title, description, color=color, fields=fields, footer_text=footer_text)


_DEFAULT_WEBHOOK_URL = _get_config_value("DISCORD_WEBHOOK_URL")

# Backward-compatible default webhook (legacy usage)
discord = DiscordAlert(_DEFAULT_WEBHOOK_URL)

# Multi-channel router
discord_router = DiscordAlertRouter()


# ============================================================================
# SPECIFIC ALERT FUNCTIONS
# ============================================================================

def alert_bot_started(mode: str = "LIVE"):
    """Alert: Bot started"""
    discord_router.send_embed(
        CHANNEL_STATUS,
        title=f"🤖 Bot Started — {mode} Mode",
        description="XAU/USD Trading Bot is online and running.",
        color=0x00ff00,
        fields=[
            {"name": "Mode", "value": mode, "inline": True},
            {"name": "Symbol", "value": config.SYMBOL, "inline": True},
            {"name": "Timeframe", "value": config.TIMEFRAME, "inline": True},
        ],
    )


def alert_bot_stopped(reason: str = "Manual"):
    """Alert: Bot stopped"""
    discord_router.send_embed(
        CHANNEL_STATUS,
        title="🛑 Bot Stopped",
        description="Trading Bot has been shut down.",
        color=0xff0000,
        fields=[
            {"name": "Reason", "value": reason, "inline": False},
        ],
    )


def check_ea_bridge_status_and_alert() -> None:
    """
    Check EA Bridge status and send alert if it transitions from ONLINE to FALLBACK.
    Should be called periodically (e.g., every analysis cycle).
    """
    global _ea_bridge_last_online, _ea_bridge_alert_sent
    
    try:
        if not getattr(config, 'USE_EA_BRIDGE', False):
            return  # EA Bridge not enabled, nothing to check
        
        from ea_bridge import is_ea_online, read_ea_status
        stale_threshold = getattr(config, 'EA_STALE_THRESHOLD_SECONDS', 60)
        
        currently_online = is_ea_online(stale_threshold)
        
        # Transition: ONLINE -> OFFLINE (FALLBACK)
        if _ea_bridge_last_online and not currently_online and not _ea_bridge_alert_sent:
            # Get last activity timestamp and file age from ea_status.json
            status = read_ea_status(stale_threshold)
            last_activity = "Unknown"
            offline_duration = "Unknown"
            file_age_seconds = "Unknown"
            
            if status and status.timestamp:
                last_activity = status.timestamp.strftime("%Y-%m-%d %H:%M:%S")
                # Use file modification time for age calculation (avoids timezone mismatch)
                # EA writes broker server time, Python uses local time — comparing them gives wrong results
                from ea_bridge import get_status_file_path
                try:
                    file_path = get_status_file_path()
                    file_mtime = datetime.fromtimestamp(os.path.getmtime(file_path))
                    delta = datetime.now() - file_mtime
                except Exception:
                    delta = datetime.now() - status.timestamp  # Fallback to old method
                file_age_seconds = f"{int(delta.total_seconds())}s"
                minutes = int(delta.total_seconds() / 60)
                if minutes < 60:
                    offline_duration = f"{minutes} minutes"
                else:
                    hours = minutes // 60
                    mins = minutes % 60
                    offline_duration = f"{hours}h {mins}m"
            
            # Send alert
            discord_router.send_embed(
                CHANNEL_STATUS,
                title="⚠️ EA Bridge OFFLINE — Fallback Active",
                description=(
                    "FlokiBridge EA has stopped responding. "
                    "Trade execution is now using Python fallback (direct MT5 API)."
                ),
                color=0xff9900,  # Orange/amber
                fields=[
                    {"name": "Last EA Activity", "value": last_activity, "inline": True},
                    {"name": "File Age", "value": file_age_seconds, "inline": True},
                    {"name": "Stale Threshold", "value": f"{stale_threshold}s", "inline": True},
                    {"name": "Execution Channel", "value": "Python Fallback (MT5 API)", "inline": False},
                    {"name": "Action Required", "value": "Re-attach FlokiBridge EA to XAUUSD chart in MT5", "inline": False},
                ],
            )
            log.warning(f"[EA BRIDGE] Transitioned to FALLBACK. Last activity: {last_activity}, file age: {file_age_seconds}")
            _ea_bridge_alert_sent = True
        
        # Transition: OFFLINE -> ONLINE (recovered)
        elif not _ea_bridge_last_online and currently_online:
            discord_router.send_embed(
                CHANNEL_STATUS,
                title="✅ EA Bridge ONLINE — Recovered",
                description="FlokiBridge EA is responding again. Trade execution restored to EA mode.",
                color=0x00ff00,
                fields=[
                    {"name": "Execution Channel", "value": "EA Bridge (FlokiBridge)", "inline": False},
                ],
            )
            log.info("[EA BRIDGE] Recovered — now ONLINE")
            _ea_bridge_alert_sent = False
        
        _ea_bridge_last_online = currently_online
        
    except Exception as e:
        log.debug(f"[EA BRIDGE] Status check error: {e}")


def alert_signal_detected(
    decision: str,
    final_score: float,
    tech_score: float,
    news_score: float,
    ml_score: float,
    confidence: str,
    brain_summary: str = "",
    current_price: Optional[float] = None,
    stop_loss: Optional[float] = None,
    take_profit: Optional[float] = None,
    scenario: Optional[str] = None,
    timestamp: Optional[str] = None,
):
    """Alert: Signal detected"""
    if "BUY" in decision:
        color = 0x00ff00
        emoji = "🟢"
    elif "SELL" in decision:
        color = 0xff0000
        emoji = "🔴"
    else:
        color = 0xffff00
        emoji = "🟡"

    scenario_value = scenario or _extract_scenario(brain_summary) or "N/A"
    timestamp = timestamp or _utc_timestamp()
    try:
        conf_val = float(confidence)
    except (ValueError, TypeError):
        conf_val = 0.0
    description = (
        f"Score: {final_score:.1f} | Confidence: {conf_val:.0f}%\n"
        f"Scenario: {scenario_value}\n"
        f"Price: {_format_price(current_price)}\n"
        f"SL: {_format_price(stop_loss)} | TP: {_format_price(take_profit)}\n"
        f"Timestamp: {timestamp}"
    )

    discord_router.send_embed(
        CHANNEL_SIGNALS,
        title=f"{emoji} {decision} SIGNAL — {config.SYMBOL.replace('USD', '/USD')}",
        description=description,
        color=color,
    )


def alert_brain_decision(
    decision: str,
    final_score: float,
    confidence: float,
    scenario: str,
    tech_score: float,
    ml_score: float,
    momentum_score: float,
    news_score: float,
    calendar_score: float,
    gpt_validation: Optional[Dict] = None,
    volatility_status: str = "NORMAL",
    mtf_trend: Optional[Dict] = None,
    volume_gate: Optional[Dict] = None,
    hold_forced: bool = False,
    original_decision: Optional[str] = None,
    hold_reason: Optional[str] = None,
):
    """Alert: Central brain decision details."""
    if hold_forced:
        title = f"⚠️ HOLD FORCED (Confidence {confidence:.0f}% < {config.BRAIN_MIN_CONFIDENCE}%)"
        description = (
            f"Original signal: {original_decision or decision} (Score {final_score:.1f})\n"
            f"Scenario: {scenario}\n"
            f"Reason: {hold_reason or 'Low confidence'}"
        )
        color = 0xf1c40f
        discord_router.send_embed(
            CHANNEL_BRAIN,
            title=title,
            description=description,
            color=color,
        )
        return

    title = f"🧠 BRAIN DECISION: {decision} (Score {final_score:.1f})"
    description = f"Confidence: {confidence:.0f}% | Scenario: {scenario}"
    fields = [
        {
            "name": "Pillars",
            "value": (
                f"Tech: {tech_score:.1f} | ML: {ml_score:.1f} | Mom: {momentum_score:.1f}\n"
                f"News: {news_score:.1f} | Cal: {calendar_score:.1f}"
            ),
            "inline": False,
        }
    ]

    if gpt_validation:
        gpt_action = gpt_validation.get("action", "CONFIRM")
        gpt_adjust = gpt_validation.get("adjustment", 0)
        sign = "+" if gpt_adjust >= 0 else ""
        gpt_text = f"{gpt_action} ({sign}{gpt_adjust})"
        fields.append({"name": "GPT Validator", "value": gpt_text, "inline": True})

    fields.append({"name": "Volatility", "value": volatility_status, "inline": True})

    if mtf_trend:
        alignment = mtf_trend.get("alignment", "UNKNOWN")
        d1 = mtf_trend.get("d1_direction", "?")
        h4 = mtf_trend.get("h4_direction", "?")
        fields.append({
            "name": "MTF Trend",
            "value": f"{alignment} (D1 {d1}, H4 {h4})",
            "inline": False,
        })

    if volume_gate:
        status = volume_gate.get("status", "UNKNOWN")
        ratio = volume_gate.get("ratio")
        ratio_text = f" ({ratio:.1f}x avg)" if isinstance(ratio, (int, float)) else ""
        fields.append({
            "name": "Volume Gate",
            "value": f"{status}{ratio_text}",
            "inline": False,
        })

    discord_router.send_embed(
        CHANNEL_BRAIN,
        title=title,
        description=description,
        color=0x3498db,
        fields=fields,
    )


def alert_dashboard_snapshot(
    status: str,
    mode: str,
    balance: float,
    equity: float,
    open_positions: str,
    today_stats: str,
    dashboard_url: str,
):
    """Alert: Dashboard snapshot."""
    description = (
        f"Status: {status} | Mode: {mode}\n"
        f"Balance: {_format_price(balance)} | Equity: {_format_price(equity)}\n"
        f"Open Positions: {open_positions}\n"
        f"Today: {today_stats}\n"
        f"🔗 {dashboard_url}"
    )

    discord_router.send_embed(
        CHANNEL_DASHBOARD,
        title="📊 FlokiWatch Dashboard",
        description=description,
        color=0x3498db,
    )


def alert_trade_executed(
    direction: str,
    ticket: int,
    lot_size: float,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    is_dry_run: bool = False,
    confidence: Optional[float] = None,
    scenario: Optional[str] = None,
    risk_amount: Optional[float] = None,
    risk_percent: Optional[float] = None,
):
    """Alert: Trade executed"""
    prefix = "🧪 [TEST] " if is_dry_run else ""
    color = 0x00ff00 if direction == "BUY" else 0xff0000
    pip_size = 0.1
    sl_pips = abs(entry_price - stop_loss) / pip_size
    tp_pips = abs(take_profit - entry_price) / pip_size

    fields = [
        {"name": "Entry", "value": _format_price(entry_price), "inline": True},
        {"name": "Lot", "value": f"{lot_size}", "inline": True},
        {"name": "SL", "value": f"{_format_price(stop_loss)} (-{sl_pips:.0f} pips)", "inline": True},
        {"name": "TP", "value": f"{_format_price(take_profit)} (+{tp_pips:.0f} pips)", "inline": True},
    ]

    if risk_amount is not None or risk_percent is not None:
        risk_text = _format_price(risk_amount)
        if risk_percent is not None:
            risk_text = f"{risk_text} ({risk_percent:.1f}%)"
        fields.append({"name": "Risk", "value": risk_text, "inline": True})

    if confidence is not None or scenario:
        conf_text = f"{confidence:.0f}%" if confidence is not None else "N/A"
        fields.append({"name": "Confidence", "value": conf_text, "inline": True})
        if scenario:
            fields.append({"name": "Scenario", "value": scenario, "inline": True})

    discord_router.send_embed(
        CHANNEL_TRADES,
        title=f"{prefix}✅ TRADE OPENED — {direction} #{ticket}",
        description="Trade opened automatically by the bot.",
        color=color,
        fields=fields,
    )


def alert_pending_fill(
    ticket: int,
    direction: str,
    fill_price: float,
    sl: Optional[float] = None,
    tp: Optional[float] = None,
    volume: Optional[float] = None,
):
    """FLO-285: Alert when a pending (LIMIT/STOP) order fills into a position."""
    color = 0x00ff00 if str(direction).upper() == "BUY" else 0xff0000
    fields = [
        {"name": "Fill", "value": _format_price(fill_price), "inline": True},
    ]
    if volume is not None:
        fields.append({"name": "Lot", "value": f"{volume}", "inline": True})
    if sl is not None:
        try:
            sl_pips = abs(float(fill_price) - float(sl)) / 0.1
            fields.append({"name": "SL", "value": f"{_format_price(sl)} ({sl_pips:.0f} pips)", "inline": True})
        except Exception:
            fields.append({"name": "SL", "value": _format_price(sl), "inline": True})
    if tp is not None:
        try:
            tp_pips = abs(float(tp) - float(fill_price)) / 0.1
            fields.append({"name": "TP", "value": f"{_format_price(tp)} (+{tp_pips:.0f} pips)", "inline": True})
        except Exception:
            fields.append({"name": "TP", "value": _format_price(tp), "inline": True})
    return discord_router.send_embed(
        CHANNEL_TRADES,
        title=f"📬 PENDING FILLED — {direction} #{ticket}",
        description="Pending order filled — position now open.",
        color=color,
        fields=fields,
    )


# FLO-419 (CEO 2026-05-03): story-format trade-close alert.
# Outcome -> Discord embed sidebar color. Outcome strings match
# executor.py's classification (WIN / LOSS / BE on a 0.5-pip dead zone)
# so this is one source of truth, not two.
_CLOSE_COLORS = {"WIN": 0x00FF00, "LOSS": 0xFF0000, "BE": 0xFFFF00}
_CLOSE_LABELS = {"WIN": "🟢 WIN", "LOSS": "🔴 LOSS", "BE": "🟡 BREAKEVEN"}
_MFE_BACKFILL_TIMEOUT_S = 3.0  # never block a notification on slow MT5


def _lookup_plan_context(ticket: int) -> Optional[Dict]:
    """Join trades.ticket -> snow_plans.trade_ticket and return the bits
    of plan context the alert renders. Returns None for legacy / EA-direct
    trades that have no Snow plan, so the alert can omit the plan section
    gracefully."""
    if not ticket:
        return None
    try:
        import sqlite3, json
        db_path = os.path.abspath(getattr(config, "HISTORY_DB_PATH", "data/history.db"))
        conn = sqlite3.connect(db_path, timeout=2)
        try:
            row = conn.execute(
                "SELECT id, plan_json FROM snow_plans WHERE trade_ticket = ? LIMIT 1",
                (int(ticket),),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        plan_id, plan_json = row[0], row[1]
        p = json.loads(plan_json) if plan_json else {}
        a = p.get("analysis") or {}
        e = p.get("entry") or {}
        return {
            "plan_id": plan_id,
            "thesis": (a.get("thesis") or "").strip(),
            "confidence": a.get("confidence"),
            "initial_sl": e.get("initial_sl"),
            "initial_tp": e.get("initial_tp"),
        }
    except Exception as ex:
        log.debug(f"alert_trade_closed | plan lookup failed for #{ticket}: {ex}")
        return None


def _lookup_mfe_mae(
    ticket: int,
    direction: str,
    entry_price: Optional[float],
    open_iso: Optional[str],
    close_iso: Optional[str],
) -> tuple:
    """Return (mfe_pips, mae_pips). Tier 1: trades table (already populated
    by record_trade_close in normal flow). Tier 2: mfe_backfill from M1 with
    a hard 3s wall-clock budget — notifications must never wait on slow MT5.
    Falls back to (None, None) on any failure or timeout."""
    # Tier 1 — DB read
    try:
        import sqlite3
        db_path = os.path.abspath(getattr(config, "HISTORY_DB_PATH", "data/history.db"))
        conn = sqlite3.connect(db_path, timeout=2)
        try:
            row = conn.execute(
                "SELECT mfe_points, mae_points FROM trades WHERE ticket = ? LIMIT 1",
                (int(ticket),),
            ).fetchone()
        finally:
            conn.close()
        if row and row[0] is not None and row[1] is not None:
            return float(row[0]), float(row[1])
    except Exception as ex:
        log.debug(f"alert_trade_closed | mfe DB read failed for #{ticket}: {ex}")

    # Tier 2 — backfill from M1, with timeout. Bail if any input is missing.
    if not (entry_price and open_iso and close_iso and direction):
        return (None, None)
    try:
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as _TE
        from mfe_backfill import backfill_mfe_mae_from_m1
        with ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(
                backfill_mfe_mae_from_m1,
                int(ticket), str(direction), float(entry_price), open_iso, close_iso,
            )
            try:
                mfe, mae = fut.result(timeout=_MFE_BACKFILL_TIMEOUT_S)
                return (mfe, mae)
            except _TE:
                log.debug(f"alert_trade_closed | mfe backfill timed out (>{_MFE_BACKFILL_TIMEOUT_S}s) for #{ticket} — degrading to N/A")
                return (None, None)
    except Exception as ex:
        log.debug(f"alert_trade_closed | mfe backfill failed for #{ticket}: {ex}")
        return (None, None)


def _compute_day_pnl_usd() -> float:
    """Sum of `profit` for trades closed since the current broker midnight
    (broker midnight = 22:00 UTC the previous day for our +2h broker, but
    we use the broker_offset_hours=3 used by executor.py for actual EEST).
    Returns 0.0 on no data."""
    try:
        import sqlite3
        from tz_utils import trading_day_broker_aligned
        broker_day = trading_day_broker_aligned(broker_offset_hours=3)
        # broker midnight `broker_day` 00:00 = UTC `broker_day` minus 3h.
        broker_midnight_utc = datetime.fromisoformat(broker_day + "T00:00:00") - timedelta(hours=3)
        cutoff_iso = broker_midnight_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        db_path = os.path.abspath(getattr(config, "HISTORY_DB_PATH", "data/history.db"))
        conn = sqlite3.connect(db_path, timeout=2)
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(profit), 0.0) FROM trades "
                "WHERE close_time IS NOT NULL AND profit IS NOT NULL "
                "  AND close_time >= ?",
                (cutoff_iso,),
            ).fetchone()
        finally:
            conn.close()
        return float(row[0] if row else 0.0)
    except Exception as ex:
        log.debug(f"alert_trade_closed | day P&L compute failed: {ex}")
        return 0.0


def _lookup_close_reason(ticket: int, fallback: str) -> str:
    """FLO-419 (CEO 2026-05-04): map a closed broker ticket to the Snow
    contingency name that actually fired the close, when available.
    Falls back to the MT5 deal reason string for legacy / EA-direct
    trades or paths Snow didn't drive (broker SL/TP, manual close,
    drawdown auto-close, timeout-close).

    Empirical motivation: PLAN-009 close alert showed "Expert Advisor"
    (the raw MT5 DEAL_REASON_EXPERT label) — useless for forensics.
    The actual close was driven by Snow's exit-side close_full action
    or by broker SL; the contingency name (e.g. "give_back_protection",
    "thesis_invalidation_sweep_failed", "duration_cap") is the
    operationally meaningful attribution.

    Query: most recent close_full / close_partial trigger for the plan
    owning this ticket, with execution_status='success'. Excludes
    `_user_cancel` (operator-driven cancels are not closes) so the
    fallback applies in that case.

    Returns:
      "Snow: <contingency_name>"  when a snow_triggers row matches
      fallback                    otherwise
    """
    if not ticket:
        return fallback
    try:
        import sqlite3
        db_path = os.path.abspath(getattr(config, "HISTORY_DB_PATH", "data/history.db"))
        conn = sqlite3.connect(db_path, timeout=2)
        try:
            row = conn.execute(
                "SELECT t.contingency_name "
                "FROM snow_triggers t "
                "JOIN snow_plans p ON p.id = t.plan_id "
                "WHERE p.trade_ticket = ? "
                "  AND t.action_type IN ('close_full', 'close_partial') "
                "  AND t.execution_status = 'success' "
                "  AND t.contingency_name != '_user_cancel' "
                "ORDER BY t.id DESC LIMIT 1",
                (int(ticket),),
            ).fetchone()
        finally:
            conn.close()
        if row and row[0]:
            cname = str(row[0]).strip()
            if cname:
                return f"Snow: {cname}"
    except Exception as ex:
        log.debug(f"alert_trade_closed | close_reason lookup failed for #{ticket}: {ex}")
    return fallback


def _format_duration_iso(open_iso: Optional[str], close_iso: Optional[str]) -> str:
    """h/m/s formatter from two UTC ISO-8601 strings. 'N/A' if either missing
    or parse fails. Pattern lifted from agent_data_builder.py:882."""
    if not open_iso or not close_iso:
        return "N/A"
    try:
        dt_o = datetime.fromisoformat(str(open_iso).replace("Z", "+00:00"))
        dt_c = datetime.fromisoformat(str(close_iso).replace("Z", "+00:00"))
        secs = int((dt_c - dt_o).total_seconds())
        if secs < 0:
            return "N/A"
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        if h > 0:
            return f"{h}h {m}m {s}s"
        if m > 0:
            return f"{m}m {s}s"
        return f"{s}s"
    except Exception:
        return "N/A"


def _classify_outcome(profit: Optional[float], outcome_arg: Optional[str]) -> str:
    """Use the outcome arg if the caller already classified (executor.py is
    the canonical source). Fallback for legacy callers: $0.50 USD dead zone
    on profit."""
    if outcome_arg in ("WIN", "LOSS", "BE"):
        return outcome_arg
    if outcome_arg == "BREAKEVEN":  # rendering label, normalize back
        return "BE"
    if profit is None:
        return "BE"
    if profit > 0.50:
        return "WIN"
    if profit < -0.50:
        return "LOSS"
    return "BE"


def _build_close_description(
    *,
    outcome: str,
    direction: str,
    plan_ctx: Optional[Dict],
    entry_price: Optional[float],
    exit_price: Optional[float],
    profit: float,
    pips: Optional[float],
    duration_str: str,
    mfe_pips: Optional[float],
    mae_pips: Optional[float],
    day_pnl: float,
    reason: Optional[str],
) -> str:
    """Story-format multi-line description body. Matches the mockup
    approved by CEO 2026-05-03."""
    lines: List[str] = []
    header_label = _CLOSE_LABELS.get(outcome, "⚪ CLOSED")
    lines.append(f"{header_label} — {direction} XAU/USD")
    lines.append("━━━━━━━━━━━━━━━━━━━")

    if plan_ctx:
        lines.append(f"📋 Plan: {plan_ctx.get('plan_id', 'N/A')}")
        thesis = plan_ctx.get("thesis") or ""
        if thesis:
            # Discord description has a 4096-char ceiling but a thesis can be 2000.
            # Trim long theses to first sentence or 200 chars to keep the card scannable.
            short = thesis.split(". ")[0]
            if len(short) > 200:
                short = short[:197] + "..."
            lines.append(f"💡 Thesis: {short}")
        conf = plan_ctx.get("confidence")
        if conf is not None:
            lines.append(f"🎯 Confidence: {int(conf)}%")
        lines.append("")

    def _px(v):
        return f"${v:,.2f}" if v is not None else "N/A"

    lines.append(f"📊 Entry: {_px(entry_price)}")
    lines.append(f"📊 Exit:  {_px(exit_price)}")
    if plan_ctx and (plan_ctx.get("initial_sl") is not None or plan_ctx.get("initial_tp") is not None):
        lines.append(f"📊 SL: {_px(plan_ctx.get('initial_sl'))} | TP: {_px(plan_ctx.get('initial_tp'))}")
    lines.append("")

    pips_str = f"{pips:+.1f} pips" if pips is not None else "N/A"
    lines.append(f"💰 P&L: {profit:+.2f} USD ({pips_str})")
    lines.append(f"⏱️ Duration: {duration_str}")
    # MFE/MAE are stored as positive magnitudes (favorable / adverse pips
    # from entry). Render MFE as +N (favorable) and MAE as -N (adverse) to
    # match the mockup convention CEO approved 2026-05-03.
    if mfe_pips is not None:
        lines.append(f"📈 Best:  +{abs(mfe_pips):.1f} pips (MFE)")
    else:
        lines.append("📈 Best:  N/A")
    if mae_pips is not None:
        lines.append(f"📉 Worst: -{abs(mae_pips):.1f} pips (MAE)")
    else:
        lines.append("📉 Worst: N/A")
    lines.append("")

    lines.append(f"📅 Day P&L: {day_pnl:+.2f} USD")
    if reason:
        lines.append(f"🏷️ Close reason: {reason}")
    return "\n".join(lines)


def alert_trade_closed(
    ticket: int,
    direction: str,
    profit: float,
    profit_percent: float,
    reason: str,
    pending: bool = False,
    outcome: str = None,
    entry_price: Optional[float] = None,
    exit_price: Optional[float] = None,
    pips: Optional[float] = None,
    duration: Optional[str] = None,
    phase: Optional[str] = None,
    open_time_iso: Optional[str] = None,
    close_time_iso: Optional[str] = None,
):
    """Alert: Trade closed.

    FLO-419 (CEO 2026-05-03): enriched story format. Joins to snow_plans
    via ticket to surface the originating plan's thesis/confidence/SL/TP,
    pulls MFE/MAE (DB tier 1, MT5 backfill tier 2 with 3s timeout), and
    appends the day's running broker-aligned P&L.

    The 5 monitor.py callers continue to pass the same kwargs they did
    pre-FLO-419 — every new field is optional and looked up internally.
    `open_time_iso` / `close_time_iso` are new optional kwargs; supplying
    them lets the alert compute duration and Tier-2 MFE/MAE backfill
    without an extra DB hop.
    """
    # Pending path stays terse — no plan join needed.
    if pending and outcome:
        if outcome == "WIN":
            color = 0x00FF00
            pnl_value = "WIN — Awaiting confirmation"
        elif outcome == "LOSS":
            color = 0xFF0000
            pnl_value = "LOSS — Awaiting confirmation"
        else:
            color = 0x95A5A6
            pnl_value = f"{outcome} — Awaiting confirmation"
        discord_router.send_embed(
            CHANNEL_TRADES,
            title=f"⏳ TRADE CLOSED — {direction} #{ticket}",
            description=f"Position closed; final P&L pending broker confirmation.\n\n**P&L:** {pnl_value}",
            color=color,
            fields=[],
        )
        return

    # Compute pips locally if caller didn't.
    if pips is None and entry_price is not None and exit_price is not None:
        pips = (exit_price - entry_price) / 0.1 if direction == "BUY" else (entry_price - exit_price) / 0.1

    classified = _classify_outcome(profit, outcome)
    color = _CLOSE_COLORS.get(classified, 0x95A5A6)

    # Enrichments. Each helper internally degrades to None / 0.0 on failure.
    plan_ctx = _lookup_plan_context(ticket)
    mfe_pips, mae_pips = _lookup_mfe_mae(ticket, direction, entry_price, open_time_iso, close_time_iso)
    day_pnl = _compute_day_pnl_usd()
    duration_str = duration or _format_duration_iso(open_time_iso, close_time_iso)
    close_reason_str = _lookup_close_reason(ticket, fallback=reason)

    description = _build_close_description(
        outcome=classified,
        direction=direction,
        plan_ctx=plan_ctx,
        entry_price=entry_price,
        exit_price=exit_price,
        profit=profit,
        pips=pips,
        duration_str=duration_str,
        mfe_pips=mfe_pips,
        mae_pips=mae_pips,
        day_pnl=day_pnl,
        reason=close_reason_str,
    )

    discord_router.send_embed(
        CHANNEL_TRADES,
        title=f"{_CLOSE_LABELS.get(classified, '⚪ CLOSED')} #{ticket}",
        description=description,
        color=color,
        fields=[],
    )


def alert_trade_resolved(ticket: int, direction: str, profit: float, profit_percent: float, reason: str):
    """Alert: Pending trade resolved with real P&L from MT5"""
    if profit >= 0:
        emoji = "✅"
        color = 0x00ff00
    else:
        emoji = "❌"
        color = 0xff0000

    discord_router.send_embed(
        CHANNEL_TRADES,
        title=f"{emoji} TRADE RESOLVED — {direction} #{ticket}",
        description="Real P&L confirmed by MT5.",
        color=color,
        fields=[
            {"name": "P&L", "value": f"{_format_price(profit)} ({profit_percent:+.1f}%)", "inline": True},
            {"name": "Reason", "value": reason, "inline": True},
        ],
    )


def alert_breakeven(
    ticket: int,
    old_sl: float,
    new_sl: float,
    profit_pips: float,
    direction: Optional[str] = None,
    entry_price: Optional[float] = None,
):
    """Alert: Breakeven activated"""
    direction_label = f"{direction} " if direction else ""
    entry_value = entry_price if entry_price is not None else new_sl
    description = (
        f"SL moved to entry ({_format_price(entry_value)})\n"
        f"Profit at trigger: {profit_pips:+.0f} pips"
    )

    discord_router.send_embed(
        CHANNEL_TRADES,
        title=f"🔒 BREAKEVEN — {direction_label}#{ticket}",
        description=description,
        color=0x00bfff,
    )


def alert_sl_hit(ticket: int, loss: float, loss_percent: float):
    """Alert: Stop Loss hit"""
    discord_router.send_embed(
        CHANNEL_TRADES,
        title=f"❌ TRADE CLOSED — #{ticket}",
        description="Stop Loss hit.",
        color=0xff0000,
        fields=[
            {"name": "Loss", "value": f"{_format_price(loss)} ({loss_percent:.1f}%)", "inline": True},
        ],
    )


def alert_safety_block(decision: str, score: float, reason: str, agent_decision: Optional[str] = None):
    """Alert: Trade blocked by safety check"""
    effective_decision = agent_decision or decision
    discord_router.send_embed(
        CHANNEL_BRAIN,
        title="⛔ Signal Blocked",
        description=f"Trade {effective_decision} (Score: {score:.1f}) was blocked.",
        color=0xffff00,
        fields=[
            {"name": "Reason", "value": reason, "inline": False},
        ],
    )


def alert_m5_reversal_block(direction: str, move_pct: float, description: str):
    """Alert: Trade blocked by M5 reversal detection"""
    discord_router.send_embed(
        CHANNEL_BRAIN,
        title="🔄 M5 Reversal — Entry Blocked",
        description=f"{direction} signal blocked: M5 price contradicts direction.",
        color=0xff6600,
        fields=[
            {"name": "M5 Move (30 min)", "value": f"{move_pct:+.2f}%", "inline": True},
            {"name": "Detail", "value": description, "inline": False},
        ],
    )


def alert_error(error_type: str, message: str, impact: str = "", severity: str = "error"):
    """Alert: error/warning/critical."""
    sev = severity.lower()

    # FLO-98: CRITICAL uses 30-min cooldown to prevent @here spam if EA Bridge
    # is persistently down. Non-critical uses standard 60s rate limit.
    if sev == "critical":
        if _rate_limited(f"critical:{error_type}", cooldown_seconds=1800):
            return
    elif _rate_limited(f"{sev}:{error_type}"):
        return

    if sev == "warning":
        emoji = "⚠️"
        color = 0xf1c40f
        label = "WARNING"
    elif sev == "critical":
        emoji = "🔴"
        color = 0xff0000
        label = "CRITICAL"
    else:
        emoji = "🚨"
        color = 0xff0000
        label = "ERROR"

    fields = [
        {"name": "Type", "value": error_type, "inline": True},
        {"name": "Message", "value": message, "inline": False},
    ]
    if impact:
        fields.append({"name": "Impact", "value": impact, "inline": False})
    fields.append({"name": "Timestamp", "value": _utc_timestamp(), "inline": False})

    # FLO-98: CRITICAL alerts include @here mention to ping the channel
    mention = "@here " if sev == "critical" else ""

    discord_router.send_embed(
        CHANNEL_ERRORS,
        title=f"{emoji} {label}",
        description=f"{mention}Alert from trading bot.",
        color=color,
        fields=fields,
    )


def alert_daily_summary(
    trades_total: int,
    wins: int,
    losses: int,
    pnl: float,
    pnl_percent: float,
    current_balance: float,
    gpt_stats: dict = None,
    best_trade: Optional[str] = None,
    worst_trade: Optional[str] = None,
    scenarios_triggered: Optional[Dict[str, int]] = None,
    signals_blocked: Optional[int] = None,
):
    """Alert: Daily summary"""
    win_rate = (wins / trades_total * 100) if trades_total > 0 else 0
    
    if pnl >= 0:
        emoji = "📈"
    else:
        emoji = "📉"
    
    fields = [
        {"name": "Trades", "value": str(trades_total), "inline": True},
        {"name": "Wins", "value": str(wins), "inline": True},
        {"name": "Losses", "value": str(losses), "inline": True},
        {"name": "Win Rate", "value": f"{win_rate:.1f}%", "inline": True},
        {"name": "P&L", "value": f"${pnl:+.2f} ({pnl_percent:+.1f}%)", "inline": True},
        {"name": "Balance", "value": f"${current_balance:.2f}", "inline": True},
    ]
    
    if gpt_stats:
        total_gpt = gpt_stats.get("confirm", 0) + gpt_stats.get("boost", 0) + gpt_stats.get("reduce", 0)
        gpt_value = (
            f"CONFIRM: {gpt_stats.get('confirm', 0)} | "
            f"BOOST: {gpt_stats.get('boost', 0)} | "
            f"REDUCE: {gpt_stats.get('reduce', 0)}\n"
            f"Total: {total_gpt} analyses (cache: {gpt_stats.get('from_cache', 0)})"
        )
        fields.append({"name": "🤖 GPT Validator", "value": gpt_value, "inline": False})
    
    if best_trade:
        fields.append({"name": "Best", "value": best_trade, "inline": False})
    if worst_trade:
        fields.append({"name": "Worst", "value": worst_trade, "inline": False})
    if scenarios_triggered:
        scenarios_text = ", ".join(
            f"{name} ({count})" for name, count in scenarios_triggered.items()
        )
        fields.append({"name": "Scenarios triggered", "value": scenarios_text, "inline": False})
    if signals_blocked is not None:
        fields.append({"name": "Signals blocked", "value": str(signals_blocked), "inline": True})

    discord_router.send_embed(
        CHANNEL_DAILY,
        title=f"{emoji} Daily Summary",
        description=f"Trading bot performance over the last 24h.",
        color=0x3498db,
        fields=fields,
    )


def alert_weekly_summary(
    week_label: str,
    trades_total: int,
    wins: int,
    losses: int,
    breakevens: int,
    pnl: float,
    profit_factor: float,
    best_day: str,
    worst_day: str,
    top_scenario: str,
    worst_scenario: str,
    live_stats: str,
):
    """Alert: Weekly summary."""
    win_rate = (wins / trades_total * 100) if trades_total > 0 else 0
    description = (
        f"Trades: {trades_total} ({wins}W / {losses}L / {breakevens}BE)\n"
        f"Win Rate: {win_rate:.1f}%\n"
        f"P&L: {_format_price(pnl)}\n"
        f"Profit Factor: {profit_factor:.2f}"
    )
    fields = [
        {"name": "Best day", "value": best_day, "inline": True},
        {"name": "Worst day", "value": worst_day, "inline": True},
        {"name": "Top scenario", "value": top_scenario, "inline": False},
        {"name": "Worst scenario", "value": worst_scenario, "inline": False},
        {"name": "Live stats", "value": live_stats, "inline": False},
    ]
    discord_router.send_embed(
        CHANNEL_WEEKLY,
        title=f"📊 Weekly Summary — {week_label}",
        description=description,
        color=0x3498db,
        fields=fields,
    )


def alert_monthly_summary(
    month_label: str,
    trades_total: int,
    win_rate: float,
    pnl: float,
    profit_factor: float,
    max_drawdown: float,
    balance_start: float,
    balance_end: float,
    note: Optional[str] = None,
):
    """Alert: Monthly summary."""
    description = (
        f"Trades: {trades_total}\n"
        f"Win Rate: {win_rate:.1f}%\n"
        f"P&L: {_format_price(pnl)}\n"
        f"Profit Factor: {profit_factor:.2f}\n"
        f"Max Drawdown: {_format_price(max_drawdown)}\n"
        f"Balance: {_format_price(balance_start)} → {_format_price(balance_end)}"
    )
    fields = []
    if note:
        fields.append({"name": "Note", "value": note, "inline": False})
    discord_router.send_embed(
        CHANNEL_MONTHLY,
        title=f"🏆 Monthly Report — {month_label}",
        description=description,
        color=0x3498db,
        fields=fields if fields else None,
    )


def alert_pause_trading(reason: str, resume_time: str):
    """Alert: Trading paused"""
    discord_router.send_embed(
        CHANNEL_STATUS,
        title="🛑 Trading Paused",
        description="Bot paused operations automatically.",
        color=0xffff00,
        fields=[
            {"name": "Reason", "value": reason, "inline": False},
            {"name": "Resumes at", "value": resume_time, "inline": False},
        ],
    )


def alert_trailing_stop(
    ticket: int,
    old_sl: float,
    new_sl: float,
    direction: Optional[str] = None,
    entry_price: Optional[float] = None,
    profit_pips: Optional[float] = None,
):
    """Alert: Trailing stop activated"""
    direction_label = f"{direction} " if direction else ""
    sl_from_entry = None
    if entry_price is not None and direction:
        if direction == "BUY":
            sl_from_entry = (new_sl - entry_price) / 0.1
        else:
            sl_from_entry = (entry_price - new_sl) / 0.1

    description_lines = [f"New SL: {_format_price(new_sl)}"]
    if sl_from_entry is not None:
        description_lines[0] += f" ({sl_from_entry:+.1f} pips from entry)"
    if profit_pips is not None:
        description_lines.append(f"Current profit: {profit_pips:+.0f} pips")

    discord_router.send_embed(
        CHANNEL_TRADES,
        title=f"📐 TRAILING UPDATE — {direction_label}#{ticket}",
        description="\n".join(description_lines),
        color=0x3498db,
    )


def alert_heartbeat_full(
    bot_name: str,
    uptime: str,
    open_positions: int,
    last_analysis_time: str,
):
    """Alert: Status heartbeat (keep-alive)."""
    fields = [
        {"name": "Bot", "value": bot_name, "inline": True},
        {"name": "Uptime", "value": uptime, "inline": True},
        {"name": "Open positions", "value": str(open_positions), "inline": True},
        {"name": "Last analysis", "value": last_analysis_time, "inline": False},
    ]
    return discord_router.send_embed(
        CHANNEL_STATUS,
        title="💤 Bot Heartbeat",
        description="Keep-alive ping from trading bot.",
        color=0x3498db,
        fields=fields,
    )


def alert_market_closed(reason: str, next_open: str):
    """Alert: Market closed"""
    discord_router.send_embed(
        CHANNEL_STATUS,
        title="Market Closed",
        description=reason,
        color=0x95a5a6,
        fields=[
            {"name": "Next open", "value": next_open, "inline": False},
        ],
    )


def alert_market_open():
    """Alert: Market opened"""
    discord_router.send_embed(
        CHANNEL_STATUS,
        title="🏪 Market Open",
        description="Gold market has reopened. Bot active and analyzing.",
        color=0x2ecc71,
    )


def alert_heartbeat_short():
    """Alert: Short heartbeat (no significant changes)"""
    timestamp = _utc_timestamp()
    return discord_router.send_embed(
        CHANNEL_STATUS,
        title="💤 Bot Heartbeat",
        description=f"Keep-alive ping — {timestamp}",
        color=0x3498db,
    )


def alert_spread_delay(spread: float, max_spread: float, retry_count: int):
    """Alert: Trade delayed due to high spread (first occurrence only)"""
    discord_router.send_embed(
        CHANNEL_BRAIN,
        title="⏳ Entry Delayed — High Spread",
        description=f"Spread too high: **{spread:.1f} pips** (max: {max_spread:.1f})\nRetrying entry...",
        color=0xf39c12,
        fields=[
            {"name": "Retry", "value": f"#{retry_count}", "inline": True},
            {"name": "Max Spread", "value": f"{max_spread:.1f} pips", "inline": True},
        ],
    )


def alert_spread_skip(direction: str, spread: float, final_score: float):
    """Alert: Trade skipped after spread timeout"""
    discord_router.send_embed(
        CHANNEL_BRAIN,
        title="⛔ Trade Skipped — Spread Timeout",
        description=f"Spread did not normalize. {direction} signal (score {final_score:.0f}) was not executed.",
        color=0xe74c3c,
        fields=[
            {"name": "Final Spread", "value": f"{spread:.1f} pips", "inline": True},
            {"name": "Reason", "value": "Rollover / Low liquidity / News spike", "inline": True},
        ],
    )


def alert_proactive_decision(agent_result) -> bool:
    decision = getattr(agent_result, "decision", "") or ""
    confidence = getattr(agent_result, "confidence", None)
    try:
        confidence_int = int(confidence) if confidence is not None else None
    except Exception:
        confidence_int = None

    reasoning = getattr(agent_result, "reasoning", "") or ""
    key_factors = getattr(agent_result, "key_factors", None) or []
    concerns = getattr(agent_result, "concerns", None) or []
    trade_plan = getattr(agent_result, "trade_plan", None)
    adjustment = getattr(agent_result, "adjustment", None)
    close_reason = getattr(agent_result, "close_reason", None)

    if confidence_int is None:
        title_suffix = ""
    else:
        title_suffix = f" ({confidence_int}%)"

    def _truncate(text: str, max_chars: int) -> str:
        if not text:
            return ""
        t = str(text).strip()
        return (t[:max_chars] + "...") if len(t) > max_chars else t

    def _as_lines(value) -> List[str]:
        if value is None:
            return []
        if isinstance(value, str):
            v = value.strip()
            return [v] if v else []
        if isinstance(value, list):
            out = []
            for item in value:
                if item is None:
                    continue
                s = str(item).strip()
                if s:
                    out.append(s)
            return out
        if isinstance(value, dict):
            out = []
            for k, v in value.items():
                if v is None:
                    continue
                s = str(v).strip()
                if not s:
                    continue
                out.append(f"{k}: {s}")
            return out
        s = str(value).strip()
        return [s] if s else []

    title_prefix_map = {
        "OPEN_BUY": "🎯 PROACTIVE: OPEN_BUY",
        "OPEN_SELL": "🎯 PROACTIVE: OPEN_SELL",
        "HOLD_TRADE": "🔄 PROACTIVE: HOLD_TRADE",
        "CLOSE_TRADE": "🚪 PROACTIVE: CLOSE_TRADE",
        "ADJUST_TRADE": "⚙️ PROACTIVE: ADJUST_TRADE",
        "WAIT": "⏳ PROACTIVE WAIT",
    }
    title = f"{title_prefix_map.get(decision, '🤖 PROACTIVE')}{title_suffix}"

    if decision in ("OPEN_BUY", "OPEN_SELL"):
        color = 0x00ff00 if decision == "OPEN_BUY" else 0xff0000
        fields: List[Dict] = []

        if trade_plan is not None:
            if isinstance(trade_plan, dict):
                entry = trade_plan.get("entry") or trade_plan.get("entry_price")
                sl = trade_plan.get("sl") or trade_plan.get("stop_loss")
                tp = trade_plan.get("tp") or trade_plan.get("take_profit") or trade_plan.get("tp1")
                rr = trade_plan.get("rr") or trade_plan.get("risk_reward")
                entry_label = trade_plan.get("entry_type") or "MARKET"

                plan_lines = []
                if entry is not None:
                    plan_lines.append(f"Entry: {entry_label} @ {entry}")
                if sl is not None:
                    plan_lines.append(f"SL: {sl}")
                if tp is not None:
                    plan_lines.append(f"TP: {tp}")
                if rr is not None:
                    plan_lines.append(f"R:R: {rr}")
                if plan_lines:
                    fields.append({"name": "Trade Plan", "value": "\n".join(plan_lines), "inline": False})
            else:
                plan_text = _truncate(str(trade_plan), 600)
                if plan_text:
                    fields.append({"name": "Trade Plan", "value": plan_text, "inline": False})

        reasoning_snip = _truncate(reasoning, 200)
        if reasoning_snip:
            fields.append({"name": "Reasoning", "value": reasoning_snip, "inline": False})

        kf_lines = _as_lines(key_factors)
        if kf_lines:
            fields.append({"name": "Key factors", "value": "\n".join(kf_lines[:6]), "inline": False})

        con_lines = _as_lines(concerns)
        if con_lines:
            fields.append({"name": "Concerns", "value": "\n".join(con_lines[:6]), "inline": False})

        return discord_router.send_embed(
            CHANNEL_BRAIN,
            title=title,
            description="Proactive agent decision (H1 snapshot).",
            color=color,
            fields=fields if fields else None,
        )

    if decision == "WAIT":
        reasoning_snip = _truncate(reasoning, 150)
        desc = reasoning_snip or "Waiting."
        return discord_router.send_embed(
            CHANNEL_BRAIN,
            title=title,
            description=desc,
            color=0xf39c12,
        )

    if decision in ("HOLD_TRADE", "CLOSE_TRADE", "ADJUST_TRADE"):
        color = 0x3498db
        if decision == "CLOSE_TRADE":
            color = 0x95a5a6
        elif decision == "ADJUST_TRADE":
            color = 0x8e44ad

        fields: List[Dict] = []
        reasoning_snip = _truncate(reasoning, 200)
        if reasoning_snip:
            fields.append({"name": "Reasoning", "value": reasoning_snip, "inline": False})

        if decision == "CLOSE_TRADE":
            cr_lines = _as_lines(close_reason)
            if cr_lines:
                fields.append({"name": "Close reason", "value": "\n".join(cr_lines[:10]), "inline": False})
        elif decision == "ADJUST_TRADE":
            adj_lines = _as_lines(adjustment)
            if adj_lines:
                fields.append({"name": "Adjustment", "value": "\n".join(adj_lines[:12]), "inline": False})
        elif decision == "HOLD_TRADE":
            hold_detail = getattr(agent_result, "hold_summary", None) or getattr(agent_result, "hold_detail", None)
            hold_lines = _as_lines(hold_detail)
            if hold_lines:
                fields.append({"name": "Hold", "value": "\n".join(hold_lines[:6]), "inline": False})

        return discord_router.send_embed(
            CHANNEL_BRAIN,
            title=title,
            description="Proactive agent decision (H1 snapshot).",
            color=color,
            fields=fields if fields else None,
        )

    # Unknown decision type: still send minimal context
    return discord_router.send_embed(
        CHANNEL_BRAIN,
        title=title,
        description=_truncate(reasoning, 200) or f"Decision: {decision}",
        color=0x95a5a6,
    )


def alert_agent_decision(
    brain_decision: str,
    brain_score: float,
    brain_confidence: float,
    agent_decision: str,
    agent_confidence: int,
    agent_reasoning: str,
    agent_key_factors: List[str],
    agent_concerns: List[str],
    agreement: bool,
    executed: str,
    mode: str = "shadow",
    latency_ms: int = 0,
    tokens_used: int = 0,
):
    """Alert: AI Agent decision (shadow mode comparison)"""
    # Determine colors and emojis
    if agent_decision in ("OPEN_BUY", "OPEN_SELL"):
        agent_emoji = "✅"
        agent_color = 0x00ff00 if "BUY" in agent_decision else 0xff0000
    elif agent_decision == "REJECT":
        agent_emoji = "❌"
        agent_color = 0xe74c3c
    elif agent_decision == "WAIT":
        agent_emoji = "⏳"
        agent_color = 0xf39c12
    else:  # DEFER_TO_BRAIN
        agent_emoji = "🔄"
        agent_color = 0x95a5a6

    agreement_emoji = "✅" if agreement else "❌"
    mode_label = mode.upper()

    # Build description
    description = (
        f"**Brain:** {brain_decision} (score {brain_score:.1f}, conf {brain_confidence:.0f}%)\n"
        f"**Agent:** {agent_emoji} {agent_decision} (conf {agent_confidence}%)\n\n"
        f"**Agreement:** {agreement_emoji} {'YES' if agreement else 'NO'}\n"
        f"**Executed:** {executed} ({mode_label} mode)"
    )

    # Build fields
    fields = []

    # Reasoning (truncate if too long)
    reasoning_display = agent_reasoning[:400] + "..." if len(agent_reasoning) > 400 else agent_reasoning
    if reasoning_display:
        fields.append({
            "name": "Agent Reasoning",
            "value": reasoning_display,
            "inline": False,
        })

    # Key factors
    if agent_key_factors:
        factors_text = "\n".join(f"• {f}" for f in agent_key_factors[:4])
        fields.append({
            "name": "Key Factors",
            "value": factors_text,
            "inline": False,
        })

    # Concerns
    if agent_concerns:
        concerns_text = "\n".join(f"• {c}" for c in agent_concerns[:3])
        fields.append({
            "name": "Concerns",
            "value": concerns_text,
            "inline": False,
        })

    # Performance stats
    fields.append({
        "name": "Performance",
        "value": f"Latency: {latency_ms}ms | Tokens: {tokens_used}",
        "inline": True,
    })

    discord_router.send_embed(
        CHANNEL_BRAIN,
        title=f"🤖 AGENT DECISION — {_utc_timestamp()}",
        description=description,
        color=agent_color,
        fields=fields,
    )


# ============================================================================
# TEST
# ============================================================================

def test_alerts():
    """Test alert sending"""
    print("=" * 60)
    print("🧪 DISCORD ALERTS TEST")
    print("=" * 60)
    
    # Simple test
    print("\n1. Sending simple alert...")
    result = discord.send("Bot connection test", alert_type="info")
    print(f"   Result: {'✅ OK' if result else '❌ Failed'}")
    
    # Embed test
    print("\n2. Sending embed...")
    result = discord.send_embed(
        title="🧪 Embed Test",
        description="This is a test of the alert system.",
        color=0x00ff00,
        fields=[
            {"name": "Field 1", "value": "Value 1", "inline": True},
            {"name": "Field 2", "value": "Value 2", "inline": True}
        ]
    )
    print(f"   Result: {'✅ OK' if result else '❌ Failed'}")
    
    print("\n✅ Test complete!")


# ============================================================================
# FLO-302: data_needs — Floki's structured self-assessment to Hermano
# ============================================================================

def _fmt_list_block(title: str, items: Optional[List[str]], bullet: str = "•") -> Optional[Dict]:
    """Render a non-empty list as a Discord embed field. Returns None if empty."""
    if not items:
        return None
    body = "\n".join(f"{bullet} {str(x)[:220]}" for x in items if str(x).strip())
    if not body.strip():
        return None
    # Discord field value cap is 1024 chars
    return {"name": title, "value": body[:1020], "inline": False}


def alert_data_needs(
    timestamp_utc: str,
    decision: Optional[str],
    confidence: Optional[int],
    data_needs: Dict,
    ticket_summary: Optional[str] = None,
    drift: Optional[List[Dict]] = None,
    boss_notes_summary: Optional[Dict[str, List[Dict[str, str]]]] = None,
) -> bool:
    """FLO-302: Send Floki's data_needs self-assessment to Hermano's Discord.

    `data_needs` must be the structured dict — FLO-315 schema:
    {followed_plan, not_called[], unavailable[], biggest_obstacle,
     self_critique, feature_requests[], tool_errors[], timeframes_skipped[],
     assessment}. Caller filters (skips empty cycles — self_critique alone
     does NOT count as signal; feature_requests DOES).

    `drift` optional dict {field_name: [{item, count, first_seen}, ...]} where
    field_name is "not_called", "unavailable", or "feature_requests". Each
    non-empty field triggers a secondary DRIFT embed labeled with the field
    name. Legacy: if a plain list is passed it's treated as not_called.

    FLO-305: `boss_notes_summary` optional dict with keys "acked" and "pending",
    each a list of {"id": str, "text": str} objects. Rendered as a single
    "Boss Notes" embed field. Omitted entirely when both lists are empty.
    """
    if not isinstance(data_needs, dict):
        log.debug(f"[alert_data_needs] ignored non-dict payload: {type(data_needs).__name__}")
        return False

    # Header line
    dec_part = (decision or "?")
    if confidence is not None:
        dec_part += f" ({confidence}%)"
    title = f"🔍 DATA_NEEDS — {timestamp_utc} — {dec_part}"

    # Description: ticket context, one line.
    desc = f"Ticket: {ticket_summary}" if ticket_summary else "Ticket: — (no position)"

    fields = []

    # FLO-310: plan-vs-action summary at the top.
    _fp = (data_needs.get("followed_plan") or "").strip().lower()
    if _fp:
        _fp_label = {
            "yes": "✅ yes",
            "yes_with_changes": "➖ yes (with changes)",
            "no": "❌ no",
        }.get(_fp, _fp)
        fields.append({"name": "Followed plan", "value": _fp_label, "inline": True})
    # FLO-306: split "Missing data" → "Not called" (skipped, available) +
    # "Unavailable" (errored / stale / doesn't exist).
    # Back-compat: if the payload still has legacy "missing_data" without
    # split fields, surface it under "Not called" — that matched its
    # observed semantics.
    _nc = data_needs.get("not_called")
    if _nc is None:
        _nc = data_needs.get("missing_data")
    f = _fmt_list_block("Not called (skipped this cycle)", _nc)
    if f: fields.append(f)
    f = _fmt_list_block("Unavailable (errored / stale / missing)", data_needs.get("unavailable"))
    if f: fields.append(f)

    tfs = data_needs.get("timeframes_skipped") or []
    if tfs:
        fields.append({"name": "Skipped timeframes", "value": ", ".join(str(t) for t in tfs)[:1020], "inline": True})

    obstacle = (data_needs.get("biggest_obstacle") or "").strip()
    if obstacle:
        fields.append({"name": "Biggest obstacle", "value": obstacle[:1020], "inline": False})

    # FLO-315: suggestions → self_critique (process reflection, string) +
    # feature_requests (new-capability asks, list). Back-compat: if the
    # payload still carries only legacy "suggestions", route it into
    # feature_requests so Hermano doesn't lose visibility mid-migration.
    critique = (data_needs.get("self_critique") or "").strip()
    if critique:
        fields.append({"name": "Self-critique", "value": critique[:1020], "inline": False})

    freq = data_needs.get("feature_requests")
    if not freq:
        freq = data_needs.get("suggestions")  # legacy fallback
    f = _fmt_list_block("Feature requests", freq)
    if f: fields.append(f)

    f = _fmt_list_block("Tool errors", data_needs.get("tool_errors"))
    if f: fields.append(f)

    assessment = (data_needs.get("assessment") or "").strip()
    if assessment:
        fields.append({"name": "Assessment", "value": assessment[:1020], "inline": False})

    # FLO-305: Boss Notes status — only renders when there's something to show.
    if boss_notes_summary:
        acked_items = boss_notes_summary.get("acked") or []
        pending_items = boss_notes_summary.get("pending") or []

        def _compact(items: List[Dict[str, str]]) -> str:
            """Render '[text1, text2, text3]' — fall back to id if text is empty."""
            if not items:
                return ""
            parts = []
            for it in items:
                txt = (it.get("text") or "").strip()
                if txt:
                    # Trim word-boundary if possible
                    short = txt if len(txt) <= 40 else txt[:37].rstrip() + "..."
                    parts.append(short)
                else:
                    parts.append(it.get("id", "?"))
            return "[" + ", ".join(parts) + "]"

        pieces = []
        if acked_items:
            pieces.append(f"acked {_compact(acked_items)}")
        if pending_items:
            pieces.append(f"pending {_compact(pending_items)}")
        if pieces:
            fields.append({
                "name": "Boss Notes",
                "value": " | ".join(pieces)[:1020],
                "inline": False,
            })

    color = 0x3b82f6  # blue — informational
    ok = discord_router.send_embed(
        CHANNEL_DATA_NEEDS,
        title=title,
        description=desc,
        color=color,
        fields=fields or None,
    )

    # Secondary DRIFT alert. FLO-306: drift is now keyed by field
    # ("not_called" vs "unavailable"). Each field renders its own labeled
    # group so Hermano can tell whether Floki is chronically skipping a
    # tool (not_called) versus chronically reporting a broken one
    # (unavailable). Legacy: if drift is a bare list, treat as not_called.
    if drift:
        if isinstance(drift, list):
            drift = {"not_called": drift}
        drift_fields: List[Dict[str, str]] = []
        for field_label, items in drift.items():
            if not items:
                continue
            lines = []
            for d in items:
                item = str(d.get("item", "?"))[:200]
                n = int(d.get("count", 0))
                first = d.get("first_seen") or "?"
                lines.append(f"• **{item}** — {n} consecutive cycles (first: {first})")
            pretty = {
                "not_called":  "Drifting items — Not called",
                "unavailable": "Drifting items — Unavailable",
            }.get(field_label, f"Drifting items — {field_label}")
            drift_fields.append({"name": pretty, "value": "\n".join(lines)[:1020], "inline": False})
        if drift_fields:
            # Description differentiates the two failure modes.
            descr_parts = []
            if drift.get("not_called"):
                descr_parts.append("Floki keeps SKIPPING these tools every cycle.")
            if drift.get("unavailable"):
                descr_parts.append("These data sources are persistently UNAVAILABLE — investigate infra.")
            discord_router.send_embed(
                CHANNEL_DATA_NEEDS,
                title=f"⚠️ DATA_NEEDS DRIFT — {timestamp_utc}",
                description=" ".join(descr_parts) or "Drift detected.",
                color=0xfbbf24,
                fields=drift_fields,
            )
    return ok


if __name__ == "__main__":
    test_alerts()
