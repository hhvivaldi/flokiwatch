"""
TIMEZONE UTILITIES — FLO-286

Single source of truth for timestamps across the bot.

RULE (CLAUDE.md Rule 22): ALL timestamps stored and served in UTC,
ISO-8601 format with explicit "Z" suffix. Display-to-local is frontend's
responsibility via window.displayTime().

Usage:
    from tz_utils import utc_iso, utc_now, trading_day_utc

    # Writing to DB / JSON (instead of datetime.now() or datetime.utcnow()):
    conn.execute("INSERT ... timestamp=?", (utc_iso(),))

    # Getting a UTC datetime object:
    now = utc_now()  # timezone-aware

    # "Today" boundary for trading-day aligned queries:
    day = trading_day_utc()  # "2026-04-13"
    conn.execute("... WHERE close_time LIKE ?", (f"{day}%",))
"""

from datetime import datetime, timezone, timedelta
from typing import Optional


def utc_now() -> datetime:
    """Timezone-aware UTC datetime. Always prefer this over datetime.utcnow()
    (which is naive and deprecated in Python 3.12+)."""
    return datetime.now(timezone.utc)


def utc_iso(dt: Optional[datetime] = None) -> str:
    """Return ISO-8601 UTC timestamp with explicit "Z" suffix.

    Examples:
        utc_iso()                          -> "2026-04-13T12:34:56.789Z"
        utc_iso(some_aware_datetime)       -> "...Z"
        utc_iso(some_naive_dt_assumed_utc) -> "...Z" (naive treated as UTC)
    """
    if dt is None:
        dt = utc_now()
    elif dt.tzinfo is None:
        # Naive datetime — assume UTC (project convention)
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        # Aware datetime — normalize to UTC
        dt = dt.astimezone(timezone.utc)
    # isoformat() on a tz-aware UTC datetime gives "...+00:00".
    # Replace with "Z" for standard/compact form the frontend expects.
    return dt.isoformat().replace("+00:00", "Z")


def to_utc_iso(value) -> str:
    """Coerce any timestamp-ish input to UTC ISO with Z suffix.

    Accepts: datetime (naive or aware), ISO string (with or without Z),
    None (returns current UTC). Returns empty string on unparseable input.

    Used by API response serializers to normalize heterogeneous DB values.
    """
    if value is None:
        return utc_iso()
    if isinstance(value, datetime):
        return utc_iso(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return ""
        # Normalize Z → +00:00 so fromisoformat can parse it
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return utc_iso(dt)
        except (ValueError, TypeError):
            # Unparseable — return as-is for visibility rather than silently dropping
            return s
    return ""


def trading_day_utc(now: Optional[datetime] = None) -> str:
    """Return the trading-day date string in UTC (YYYY-MM-DD).

    Trading day convention: the forex week starts Sunday 22:00 UTC.
    For filtering "trades today", we use the UTC calendar day. Callers
    that need broker-time alignment (midnight = 22:00 UTC boundary)
    should use `trading_day_broker_aligned()` instead.

    This is the canonical "today" boundary — replaces scattered
    datetime.now().date() and datetime.utcnow().strftime() calls.
    """
    if now is None:
        now = utc_now()
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    return now.strftime("%Y-%m-%d")


def trading_day_broker_aligned(now: Optional[datetime] = None, broker_offset_hours: int = 2) -> str:
    """Return the trading-day date aligned to broker midnight boundary.

    Broker runs UTC+N (typically +2 EET or +3 EEST). "Broker midnight"
    = 00:00 broker time = (24 - N):00 UTC on the previous day.

    Example with broker_offset_hours=2:
        UTC 21:59 Sunday → broker 23:59 Sunday → returns Sunday's date
        UTC 22:00 Sunday → broker 00:00 Monday → returns Monday's date

    Use this for daily_stats resets and "trades today" counters that
    should align with the broker's trading calendar, not the UTC calendar.
    """
    if now is None:
        now = utc_now()
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    broker_time = now + timedelta(hours=broker_offset_hours)
    return broker_time.strftime("%Y-%m-%d")
