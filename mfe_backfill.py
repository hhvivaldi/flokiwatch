"""
MFE/MAE backfill from MT5 M1 candles — FLO-287.

Tier-3 fallback when in-memory tracker (tier 1) and trade_snapshots table
(tier 2) both lack data. This happens when a trade opens and closes during
a long Floki cycle, so neither the Brain nor the monitor ever observed
the position.

Timezone handling:
- MT5 deal.time and copy_rates_range both use "broker-interpreted unix
  timestamps", which on a Windows box in CEST get rendered as broker wall
  clock via `datetime.fromtimestamp()`. We stay in that naive broker-time
  frame the whole way through — no UTC conversion, no config offset.
- If we have no deal history (trade too old), we fall back to the DB's
  open_time by computing the dynamic broker offset from the current tick.
"""
import time as _time
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple

from mt5_safe import mt5  # FLO-348: thread-safe MT5 proxy

from logger import log


_PIP_SIZE = 0.1  # XAUUSD


def _parse_iso_to_utc(iso_str: str) -> Optional[datetime]:
    """Parse ISO-8601 string (with or without Z) to a tz-aware UTC datetime."""
    if not iso_str:
        return None
    try:
        s = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _utc_to_broker_naive(dt_utc: datetime) -> Optional[datetime]:
    """Convert a tz-aware UTC datetime to a NAIVE datetime that, when passed to
    MT5 API calls like copy_rates_range / history_deals_get, selects data at
    the trade's real UTC moment.

    Why the unusual math: MT5 stores times as "broker_wall_as_utc unix" (e.g.
    broker EEST 16:50 stored as unix(16:50 treated as UTC) = 1776099013 for
    a trade that actually occurred at 13:50 UTC). The Python API converts a
    naive datetime argument by treating it as local (box) time. So to select
    bars for the trade, the naive datetime must render as `fromtimestamp`
    of the broker-stored unix.
    """
    try:
        tick = mt5.symbol_info_tick("XAUUSD")
        _src = "tick.time"
        if tick and tick.time:
            server_offset_s = int(tick.time) - int(_time.time())
        else:
            server_offset_s = 10800
            _src = "constant"
        if not (7200 <= server_offset_s <= 14400):
            server_offset_s = 10800
            _src = "FALLBACK"
        broker_unix = int(dt_utc.timestamp()) + server_offset_s
        broker_naive = datetime.fromtimestamp(broker_unix)
        try:
            log.info(
                "TIMEZONE_AUDIT | offset={}s ({}) | utc={} | broker={} | site=mfe_backfill._utc_to_broker_naive".format(
                    server_offset_s, _src,
                    dt_utc.strftime("%H:%M:%S"),
                    broker_naive.strftime("%H:%M:%S"),
                )
            )
        except Exception:
            pass
        return broker_naive
    except Exception:
        pass
    return None


def _find_mt5_fill_broker_dt(ticket: int, open_dt_utc: datetime, close_dt_utc: datetime) -> Optional[datetime]:
    """Query MT5 deal history for the entry deal of `ticket`. Returns naive
    broker-time datetime from `fromtimestamp(d.time)` or None if not found."""
    try:
        # history_deals_get accepts broker-naive datetimes. Convert and pad 1 min.
        from_broker = _utc_to_broker_naive(open_dt_utc - timedelta(minutes=1))
        to_broker = _utc_to_broker_naive(close_dt_utc + timedelta(minutes=1))
        if not from_broker or not to_broker:
            return None
        deals = mt5.history_deals_get(from_broker, to_broker, group="XAUUSD")
        if not deals:
            return None
        for d in deals:
            if getattr(d, "position_id", 0) == int(ticket) and getattr(d, "entry", None) == 0:
                # fromtimestamp on a box in CEST treats d.time as local;
                # matches broker wall-clock since MT5 stores broker-interpreted unix.
                return datetime.fromtimestamp(d.time)
    except Exception as e:
        log.debug(f"MFE_BACKFILL | deal lookup failed for #{ticket}: {e}")
    return None


def backfill_mfe_mae_from_m1(
    ticket: int,
    direction: str,
    entry: float,
    open_iso: str,
    close_iso: str,
    symbol: str = "XAUUSD",
) -> Tuple[Optional[float], Optional[float]]:
    """Compute MFE/MAE in pips from MT5 M1 candles between trade fill and close.

    Returns (mfe_points, mae_points) — signed, following the existing convention
    where MFE/MAE are observed profit_points extremes (either sign allowed).
    Returns (None, None) on any failure — caller should leave columns NULL.
    """
    if not ticket or entry is None or not direction:
        return (None, None)

    direction = direction.upper()
    if direction not in ("BUY", "SELL"):
        return (None, None)

    open_dt_utc = _parse_iso_to_utc(open_iso)
    close_dt_utc = _parse_iso_to_utc(close_iso)
    if not open_dt_utc or not close_dt_utc:
        return (None, None)

    # Prefer MT5 deal-history fill time (correct for pending orders); fall back
    # to the DB's open_time (correct for market orders — dynamic broker offset).
    fill_broker = _find_mt5_fill_broker_dt(ticket, open_dt_utc, close_dt_utc)
    if fill_broker is None:
        fill_broker = _utc_to_broker_naive(open_dt_utc)
    close_broker = _utc_to_broker_naive(close_dt_utc)
    if fill_broker is None or close_broker is None:
        return (None, None)

    # Pad by one minute on each side so the boundary M1 candles are included.
    start = fill_broker - timedelta(seconds=60)
    end = close_broker + timedelta(seconds=60)
    try:
        bars = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M1, start, end)
    except Exception as e:
        log.debug(f"MFE_BACKFILL | copy_rates_range failed for #{ticket}: {e}")
        return (None, None)
    if bars is None or len(bars) == 0:
        return (None, None)

    # Filter bars that overlap [fill_broker, close_broker]. bar[0] is the candle
    # OPEN unix in the same broker-naive frame (matching fromtimestamp).
    fill_u = fill_broker.timestamp()
    close_u = close_broker.timestamp()
    in_trade = [b for b in bars if (int(b[0]) < close_u) and (int(b[0]) + 60 > fill_u)]
    if not in_trade:
        return (None, None)

    highs = [float(b[2]) for b in in_trade]
    lows = [float(b[3]) for b in in_trade]
    max_high = max(highs)
    min_low = min(lows)

    if direction == "BUY":
        mfe_price = max_high - float(entry)
        mae_price = min_low - float(entry)
    else:  # SELL
        mfe_price = float(entry) - min_low
        mae_price = float(entry) - max_high

    mfe_points = round(mfe_price / _PIP_SIZE, 1)
    mae_points = round(mae_price / _PIP_SIZE, 1)

    log.info(
        f"MFE_BACKFILL | ticket=#{ticket} | bars={len(in_trade)} | "
        f"max_high={max_high:.2f} min_low={min_low:.2f} entry={float(entry):.2f} | "
        f"MFE={mfe_points:+.1f}p MAE={mae_points:+.1f}p (from_mt5_m1)"
    )
    return (mfe_points, mae_points)
