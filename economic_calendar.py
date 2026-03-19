"""
ECONOMIC CALENDAR - 5th Pillar of the Central Brain
Provides Calendar Score (0-100) and Calendar Bias for the brain.

Data sources (by priority):
1. MQL5 Bridge: JSON exported by CalendarExporter.mq5 Service
2. FCS API: fcsapi.com (fallback if JSON unavailable)
3. Hardcoded Schedule: recurring dates of the 5 main events (final fallback)

Score:
- 0   = Maximum risk (during release)
- 20  = Caution (pre-event <30 min)
- 50  = Neutral (no relevant events)
- 85  = Opportunity (post-event with clear bias)

Phases:
- NORMAL:     No relevant upcoming events
- PRE_EVENT:  <30 min before high-impact event
- DURING:     0-3 min after release
- POST_EVENT: 3-30 min after release
"""

import json
import os
import requests
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import config
from logger import log


# ============================================================================
# CONSTANTS
# ============================================================================

PHASES = {
    "NORMAL": "normal",
    "PRE_EVENT": "pre_event",
    "DURING": "during",
    "POST_EVENT": "post_event",
}

# USD high-impact events we care about (EN + ES for MT5 multi-language)
HIGH_IMPACT_EVENTS = [
    # English
    "nonfarm payrolls",
    "non-farm payrolls",
    "nonfarm employment change",
    "consumer price index",
    "cpi",
    "core cpi",
    "fed interest rate decision",
    "federal funds rate",
    "fomc statement",
    "fomc minutes",
    "fomc press conference",
    "initial jobless claims",
    "unemployment claims",
    "existing home sales",
    "bond auction",
    "treasury auction",
    # Español (MT5 in Spanish)
    "nóminas no agrícolas",
    "nominas no agricolas",
    "cambio de empleo no agrícola",
    "índice de precios al consumo",
    "indice de precios al consumo",
    "ipc",
    "ipc subyacente",
    "decisión de tipos de interés",
    "decision de tipos de interes",
    "tasa de fondos federales",
    "declaración del fomc",
    "declaracion del fomc",
    "minutas del fomc",
    "conferencia de prensa del fomc",
    "peticiones iniciales del subsidio de desempleo",
    "peticiones de subsidio de desempleo",
    "subsidio de desempleo",
    "ventas de viviendas existentes",
]

# Bias rules per event type
# Key: substring of event name (lowercase)
# Value: "higher_bearish" = actual > forecast → strong USD → bearish gold
#         "higher_bullish" = actual > forecast → bullish gold
#         "dovish_bullish" = dovish tone → bullish gold (for FOMC/Fed)
EVENT_BIAS_RULES = {
    # English
    "nonfarm": "higher_bearish",       # Strong NFP → strong USD → bearish gold
    "non-farm": "higher_bearish",
    "consumer price": "higher_bearish", # High CPI → Fed hawkish → bearish gold
    "cpi": "higher_bearish",
    "core cpi": "higher_bearish",
    "fed interest rate": "higher_bearish",  # Rate hike → bearish gold
    "federal funds rate": "higher_bearish",
    "fomc": "sentiment_based",         # FOMC: depends on tone (use headlines)
    "initial jobless": "higher_bullish",  # More unemployment → weak USD → bullish gold
    "unemployment claims": "higher_bullish",
    "existing home sales": "higher_bearish",  # Strong housing → strong USD → bearish gold
    # Español
    "nóminas no agrícolas": "higher_bearish",
    "nominas no agricolas": "higher_bearish",
    "cambio de empleo no agr": "higher_bearish",
    "precios al consumo": "higher_bearish",
    "ipc": "higher_bearish",
    "tipos de interés": "higher_bearish",
    "tipos de interes": "higher_bearish",
    "tasa de fondos federales": "higher_bearish",
    "declaración del fomc": "sentiment_based",
    "declaracion del fomc": "sentiment_based",
    "minutas del fomc": "sentiment_based",
    "conferencia de prensa del fomc": "sentiment_based",
    "peticiones iniciales": "higher_bullish",
    "subsidio de desempleo": "higher_bullish",
    "ventas de viviendas": "higher_bearish",
}


# ============================================================================
# CACHE
# ============================================================================

_calendar_cache = {
    "data": None,
    "last_update": None,
}


# ============================================================================
# SOURCE 1: MQL5 BRIDGE (JSON exported by CalendarExporter.mq5)
# ============================================================================

def _read_mt5_calendar_file() -> Optional[Dict]:
    """
    Read the JSON exported by CalendarExporter.mq5 Service.
    
    Returns:
        Dict with calendar data, or None if unavailable/stale
    """
    json_path = config.CALENDAR_JSON_PATH
    
    if not os.path.exists(json_path):
        log.debug("Calendar JSON not found (MQL5 Service not active?)")
        return None
    
    try:
        # Check file age
        file_mtime = datetime.fromtimestamp(os.path.getmtime(json_path))
        age_minutes = (datetime.now() - file_mtime).total_seconds() / 60
        
        if age_minutes > config.CALENDAR_JSON_MAX_AGE_MINUTES:
            log.debug(f"Calendar JSON stale ({age_minutes:.0f} min > {config.CALENDAR_JSON_MAX_AGE_MINUTES} min)")
            return None
        
        with open(json_path, 'rb') as f:
            raw = f.read().decode('utf-8', errors='surrogatepass')
        # Strip any surrogates before JSON parsing
        raw = raw.encode('utf-8', errors='surrogatepass').decode('utf-8', errors='replace')
        data = json.loads(raw)
        
        # Validate structure
        if "events" not in data or "exported_at" not in data:
            log.warning("Calendar JSON with invalid structure")
            return None
        
        data["source"] = "mt5_bridge"
        data["file_age_minutes"] = round(age_minutes, 1)
        
        return data
        
    except (json.JSONDecodeError, IOError) as e:
        log.warning(f"Error reading Calendar JSON: {e}")
        return None


# ============================================================================
# SOURCE 2: FCS API (fallback)
# ============================================================================

def _fetch_fcs_api() -> Optional[Dict]:
    """
    Fetch calendar events via FCS API (fcsapi.com).
    Fallback when MQL5 bridge is not available.
    
    Returns:
        Dict with calendar data, or None if unavailable
    """
    api_key = config.FCS_API_KEY
    if not api_key:
        log.debug("FCS API key not configured, skipping fallback")
        return None
    
    try:
        url = "https://fcsapi.com/api-v3/forex/economy_cal"
        params = {
            "country": "US",
            "access_key": api_key,
        }
        
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        result = response.json()
        
        if result.get("status") is not True:
            log.warning(f"FCS API error: {result.get('msg', 'unknown')}")
            return None
        
        # Convert to internal format
        events = []
        for item in result.get("response", []):
            # Filter only HIGH impact
            impact = item.get("impact", "").upper()
            if impact not in ("HIGH", "3"):
                continue
            
            # Parse date
            event_date = item.get("date", "")
            event_time = item.get("time", "")
            try:
                dt = datetime.strptime(f"{event_date} {event_time}", "%Y-%m-%d %H:%M")
            except (ValueError, TypeError):
                continue
            
            events.append({
                "event_id": hash(f"{item.get('title', '')}{event_date}{event_time}"),
                "name": item.get("title", ""),
                "time_server": dt.strftime("%Y.%m.%d %H:%M:%S"),
                "importance": "HIGH",
                "currency": "USD",
                "actual_value": _parse_fcs_value(item.get("actual")),
                "forecast_value": _parse_fcs_value(item.get("forecast")),
                "previous_value": _parse_fcs_value(item.get("previous")),
            })
        
        return {
            "exported_at": datetime.utcnow().strftime("%Y.%m.%d %H:%M:%S"),
            "events_count": len(events),
            "events": events,
            "source": "fcs_api",
            "refresh_seconds": 300,
        }
        
    except Exception as e:
        log.warning(f"FCS API failed: {e}")
        return None


def _parse_fcs_value(val) -> Optional[float]:
    """Parse FCS API value (can be string, float, or empty)"""
    if val is None or val == "" or val == "null":
        return None
    try:
        # Remove non-numeric characters (%, K, M, etc.)
        clean = str(val).replace("%", "").replace("K", "").replace("M", "").replace(",", "").strip()
        return float(clean)
    except (ValueError, TypeError):
        return None


# ============================================================================
# SOURCE 3: HARDCODED SCHEDULE (final fallback)
# ============================================================================

def _get_hardcoded_schedule() -> Dict:
    """
    Return hardcoded schedule of recurring events.
    Final fallback when neither MQL5 bridge nor FCS API are available.
    
    Note: No actual/forecast — only timing for PRE_EVENT/DURING phase.
    """
    now = datetime.utcnow()
    events = []
    
    # Recurring events with typical times (UTC)
    # NFP: 1st Friday of month, 13:30 UTC
    # CPI: ~10-15 of month, 13:30 UTC
    # Fed Rate: ~8 times/year (FOMC meetings)
    # Jobless Claims: every Thursday, 13:30 UTC
    
    # Check if today is Thursday (Jobless Claims)
    if now.weekday() == 3:  # Thursday
        claims_time = now.replace(hour=13, minute=30, second=0, microsecond=0)
        events.append({
            "event_id": 0,
            "name": "Initial Jobless Claims",
            "time_server": claims_time.strftime("%Y.%m.%d %H:%M:%S"),
            "importance": "HIGH",
            "currency": "USD",
            "actual_value": None,
            "forecast_value": None,
            "previous_value": None,
        })
    
    # Check if 1st Friday of month (NFP)
    if now.weekday() == 4 and now.day <= 7:  # Friday, day 1-7
        nfp_time = now.replace(hour=13, minute=30, second=0, microsecond=0)
        events.append({
            "event_id": 0,
            "name": "Nonfarm Payrolls",
            "time_server": nfp_time.strftime("%Y.%m.%d %H:%M:%S"),
            "importance": "HIGH",
            "currency": "USD",
            "actual_value": None,
            "forecast_value": None,
            "previous_value": None,
        })
    
    # CPI: typically 10-15 of month, 13:30 UTC
    if 10 <= now.day <= 15:
        cpi_time = now.replace(hour=13, minute=30, second=0, microsecond=0)
        events.append({
            "event_id": 0,
            "name": "Consumer Price Index (CPI)",
            "time_server": cpi_time.strftime("%Y.%m.%d %H:%M:%S"),
            "importance": "HIGH",
            "currency": "USD",
            "actual_value": None,
            "forecast_value": None,
            "previous_value": None,
        })
    
    return {
        "exported_at": now.strftime("%Y.%m.%d %H:%M:%S"),
        "events_count": len(events),
        "events": events,
        "source": "hardcoded_schedule",
        "refresh_seconds": 300,
    }


# ============================================================================
# MAIN LOGIC
# ============================================================================

def _get_raw_calendar_data() -> Dict:
    """
    Get calendar data from the best available source.
    Priority: MQL5 Bridge → FCS API → Hardcoded Schedule
    """
    # Try MQL5 Bridge
    data = _read_mt5_calendar_file()
    if data is not None:
        return data
    
    # Try FCS API
    data = _fetch_fcs_api()
    if data is not None:
        return data
    
    # Fallback: hardcoded schedule
    return _get_hardcoded_schedule()


def _parse_event_time(time_str: str) -> Optional[datetime]:
    """Parse calendar time string (MT5 format: YYYY.MM.DD HH:MM:SS)"""
    for fmt in ("%Y.%m.%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y.%m.%d %H:%M"):
        try:
            return datetime.strptime(time_str, fmt)
        except (ValueError, TypeError):
            continue
    return None


def _get_reference_time(raw_data: Dict) -> datetime:
    """Return the reference clock to compare calendar events against.

    Preferred: MT5 JSON `server_time` (same timezone as `time_server`).
    Fallback: derive a server-like clock from UTC using MT5_SERVER_UTC_OFFSET.
    """
    server_time_str = raw_data.get("server_time", "") if isinstance(raw_data, dict) else ""
    if server_time_str:
        parsed = _parse_event_time(server_time_str)
        if parsed is not None:
            return parsed

    try:
        off = int(getattr(config, "MT5_SERVER_UTC_OFFSET", 0) or 0)
    except Exception:
        off = 0
    # Use local wall clock + offset, not utcnow().
    # Rationale: many deployments run on a machine configured in the same timezone as the MT5 server.
    # Using utcnow() here double-applies the offset and produces incorrect countdowns.
    return datetime.now() + timedelta(hours=off)


def _get_server_utc_offset_hours(raw_data: Dict) -> int:
    """Best-effort server UTC offset for debug/visibility.

    If MT5 provides `server_time` and `exported_at`, derive the offset.
    Otherwise fall back to config.MT5_SERVER_UTC_OFFSET.
    """
    try:
        if not isinstance(raw_data, dict):
            raw_data = {}

        st = raw_data.get("server_time", "")
        ea = raw_data.get("exported_at", "")
        if st and ea:
            st_dt = _parse_event_time(str(st))
            ea_dt = _parse_event_time(str(ea))
            if st_dt is not None and ea_dt is not None:
                # exported_at is written by Python as UTC, but may be provided by other sources.
                # Treat it as UTC if it looks like UTC (best-effort).
                utc_dt = ea_dt
                # Derive hour offset between server wall clock and exported_at.
                diff = st_dt - utc_dt
                return int(round(diff.total_seconds() / 3600.0))
    except Exception:
        pass

    try:
        return int(getattr(config, "MT5_SERVER_UTC_OFFSET", 0) or 0)
    except Exception:
        return 0


def _is_relevant_event(event_name: str) -> bool:
    """Check if the event is one of the high-impact ones we care about"""
    name_lower = event_name.lower()
    for keyword in HIGH_IMPACT_EVENTS:
        if keyword in name_lower:
            return True
    return False


def classify_phase(events: List[Dict], reference_time: Optional[datetime] = None) -> Tuple[str, Optional[Dict]]:
    """
    Classify the current phase based on upcoming events.
    
    Args:
        events: List of calendar events
        reference_time: Reference time (same timezone as events).
                       If None, uses datetime.utcnow().
    
    Returns:
        Tuple: (phase, closest_event)
        phase: "normal", "pre_event", "during", "post_event"
        closest_event: Dict of most relevant event, or None
    """
    now = reference_time if reference_time is not None else datetime.utcnow()
    pre_minutes = config.CALENDAR_PRE_EVENT_MINUTES
    during_minutes = config.CALENDAR_DURING_MINUTES
    post_minutes = config.CALENDAR_POST_EVENT_MINUTES
    
    closest_event = None
    closest_phase = PHASES["NORMAL"]
    closest_distance = float('inf')  # Temporal distance to closest event
    
    for event in events:
        event_time = _parse_event_time(event.get("time_server", ""))
        if event_time is None:
            continue
        
        # Only consider relevant events
        if not _is_relevant_event(event.get("name", "")):
            continue
        
        minutes_until = (event_time - now).total_seconds() / 60
        minutes_since = -minutes_until
        abs_distance = abs(minutes_until)
        
        # Determine phase for this event
        if 0 < minutes_until <= pre_minutes:
            phase = PHASES["PRE_EVENT"]
        elif 0 <= minutes_since <= during_minutes:
            phase = PHASES["DURING"]
        elif during_minutes < minutes_since <= post_minutes:
            phase = PHASES["POST_EVENT"]
        else:
            continue  # Event outside any relevant window
        
        # Keep the closest/most relevant event
        if abs_distance < closest_distance:
            closest_distance = abs_distance
            closest_phase = phase
            closest_event = event
    
    return closest_phase, closest_event


def _derive_bias(event: Optional[Dict]) -> str:
    """
    Derive bias (BULLISH/BEARISH/NEUTRAL) based on event and its values.
    
    Rules:
    - CPI/NFP: actual > forecast → strong USD → BEARISH gold
    - Jobless Claims: actual > forecast → weak USD → BULLISH gold
    - FOMC: no actual/forecast → NEUTRAL (depends on sentiment)
    - No data: NEUTRAL
    """
    if event is None:
        return "NEUTRAL"
    
    event_name = event.get("name", "").lower()
    actual = event.get("actual_value")
    forecast = event.get("forecast_value")
    
    # No actual or forecast → cannot derive bias
    if actual is None or forecast is None:
        return "NEUTRAL"
    
    # Find applicable rule
    rule = None
    for keyword, bias_rule in EVENT_BIAS_RULES.items():
        if keyword in event_name:
            rule = bias_rule
            break
    
    if rule is None:
        return "NEUTRAL"
    
    if rule == "sentiment_based":
        # FOMC: cannot derive automatically without sentiment
        return "NEUTRAL"
    
    # Compare actual vs forecast
    diff = actual - forecast
    threshold = abs(forecast) * 0.01 if forecast != 0 else 0.1  # 1% margin
    
    if abs(diff) < threshold:
        return "NEUTRAL"  # In line with expectations
    
    if rule == "higher_bearish":
        return "BEARISH" if diff > 0 else "BULLISH"
    elif rule == "higher_bullish":
        return "BULLISH" if diff > 0 else "BEARISH"
    
    return "NEUTRAL"


def get_calendar_score(phase: str, bias: str) -> float:
    """
    Calculate Calendar Score (0-100) based on phase and bias.
    
    Scores:
    - NORMAL: 50 (neutral)
    - PRE_EVENT: 20 (caution)
    - DURING: 0 (maximum risk)
    - POST_EVENT + clear bias: 70-85 (opportunity)
    - POST_EVENT + no bias: 45 (slightly cautious)
    """
    if phase == PHASES["NORMAL"]:
        return 50.0
    
    if phase == PHASES["PRE_EVENT"]:
        return 20.0
    
    if phase == PHASES["DURING"]:
        return 0.0
    
    if phase == PHASES["POST_EVENT"]:
        if bias == "BULLISH":
            return 85.0  # Bullish opportunity
        elif bias == "BEARISH":
            return 15.0  # Bearish opportunity (low score = bearish)
        else:
            return 45.0  # Post-event without clear bias
    
    return 50.0


def get_calendar_data() -> Dict:
    """
    Main function: returns complete calendar data for the brain.
    Uses internal cache to avoid excessive reads.
    
    Returns:
        Dict with:
        - score: 0-100
        - bias: BULLISH/BEARISH/NEUTRAL
        - phase: normal/pre_event/during/post_event
        - phase_description: readable description
        - events: list of events
        - closest_event: closest event (or None)
        - source: mt5_bridge/fcs_api/hardcoded_schedule
        - error: None or error message
    """
    global _calendar_cache
    
    now = datetime.now()
    
    # Check cache
    if (_calendar_cache["data"] is not None and 
        _calendar_cache["last_update"] is not None):
        cache_age = (now - _calendar_cache["last_update"]).total_seconds() / 60
        if cache_age < config.CALENDAR_CACHE_MINUTES:
            return _calendar_cache["data"]
    
    try:
        # Get raw data
        raw_data = _get_raw_calendar_data()
        events = raw_data.get("events", [])
        source = raw_data.get("source", "unknown")
        
        reference_time = _get_reference_time(raw_data)
        server_utc_offset_hours = _get_server_utc_offset_hours(raw_data)
        try:
            log.debug(f"Calendar reference_time (server): {reference_time}")
        except Exception:
            pass
        
        # Classify phase
        phase, closest_event = classify_phase(events, reference_time=reference_time)
        
        # Debug log: which event triggered the phase
        if closest_event and phase != PHASES["NORMAL"]:
            ev_time = _parse_event_time(closest_event.get("time_server", ""))
            if ev_time and reference_time:
                diff_min = (ev_time - reference_time).total_seconds() / 60
                if diff_min > 0:
                    log.debug(f"Calendar phase: {phase} → {closest_event['name']} (in {diff_min:.0f} min) | ref: {reference_time.strftime('%H:%M')} server")
                else:
                    log.debug(f"Calendar phase: {phase} → {closest_event['name']} ({abs(diff_min):.0f} min ago) | ref: {reference_time.strftime('%H:%M')} server")
        else:
            log.debug(f"Calendar phase: {phase} (no events in window)")
        
        # Derive bias
        bias = _derive_bias(closest_event)
        
        # Calculate score
        score = get_calendar_score(phase, bias)
        
        # Phase description
        phase_descriptions = {
            PHASES["NORMAL"]: "No relevant upcoming events",
            PHASES["PRE_EVENT"]: f"Pre-event: {closest_event['name'] if closest_event else '?'} soon",
            PHASES["DURING"]: f"DURING RELEASE: {closest_event['name'] if closest_event else '?'}",
            PHASES["POST_EVENT"]: f"Post-event: {closest_event['name'] if closest_event else '?'} (bias: {bias})",
        }
        
        result = {
            "score": score,
            "bias": bias,
            "phase": phase,
            "phase_description": phase_descriptions.get(phase, "Unknown"),
            "events": events,
            "events_count": len(events),
            "closest_event": closest_event,
            "source": source,
            "error": None,
        }
        
        # Update cache
        _calendar_cache["data"] = result
        _calendar_cache["last_update"] = now
        
        return result
        
    except Exception as e:
        log.warning(f"Economic calendar error: {e}")
        
        # Return neutral on error
        fallback = {
            "score": 50.0,
            "bias": "NEUTRAL",
            "phase": PHASES["NORMAL"],
            "phase_description": "Calendar error - neutral mode",
            "events": [],
            "events_count": 0,
            "closest_event": None,
            "source": "error_fallback",
            "error": str(e),
        }
        
        _calendar_cache["data"] = fallback
        _calendar_cache["last_update"] = now
        
        return fallback


# ============================================================================
# CONVENIENCE FUNCTIONS
# ============================================================================

def get_calendar_score_simple() -> float:
    """Return only the score (0-100)"""
    return get_calendar_data()["score"]


def get_calendar_bias_simple() -> str:
    """Return only the bias (BULLISH/BEARISH/NEUTRAL)"""
    return get_calendar_data()["bias"]


def get_calendar_phase() -> str:
    """Return only the current phase"""
    return get_calendar_data()["phase"]


def get_upcoming_events(max_events: int = 5) -> List[Dict]:
    """
    Return upcoming calendar events for dashboard visibility.
    Includes HIGH + MEDIUM importance (MEDIUM = display only, does not affect score/phases).
    
    classify_phase() and get_calendar_score() still react ONLY to HIGH.
    
    Returns:
        List of dicts with: name, time, importance, time_until, is_past
    """
    try:
        raw_data = _get_raw_calendar_data()
        events = raw_data.get("events", [])
        
        reference_time = _get_reference_time(raw_data)
        server_utc_offset_hours = _get_server_utc_offset_hours(raw_data)
        
        upcoming = []
        for event in events:
            event_time = _parse_event_time(event.get("time_server", ""))
            if event_time is None:
                continue
            
            importance = event.get("importance", "MEDIUM")
            minutes_until = (event_time - reference_time).total_seconds() / 60
            
            # Include: future + last 60 min (recent post-event)
            if minutes_until < -60:
                continue
            
            # Format time_until
            if minutes_until > 0:
                if minutes_until < 60:
                    time_until = f"{int(minutes_until)}m"
                elif minutes_until < 1440:
                    hours = int(minutes_until // 60)
                    mins = int(minutes_until % 60)
                    time_until = f"{hours}h {mins}m" if mins > 0 else f"{hours}h"
                else:
                    time_until = f"{int(minutes_until // 1440)}d"
                is_past = False
            else:
                mins_ago = int(abs(minutes_until))
                time_until = f"{mins_ago}m ago"
                is_past = True
            
            upcoming.append({
                "name": event.get("name", "?"),
                "time": event_time.strftime("%H:%M"),
                "importance": importance,
                "time_until": time_until,
                "is_past": is_past,
                "minutes_until": round(minutes_until, 1),
                "reference_time": reference_time.strftime("%Y-%m-%d %H:%M:%S"),
                "server_utc_offset_hours": server_utc_offset_hours,
            })

        # Safety: ensure we don't accidentally drop near-term HIGH events due to feed quirks.
        # If a HIGH event is within the +/-60m window, it should be eligible for display.
        try:
            for event in events:
                event_time = _parse_event_time(event.get("time_server", ""))
                if event_time is None:
                    continue
                importance = str(event.get("importance", "MEDIUM") or "MEDIUM")
                if str(importance).upper() != "HIGH":
                    continue
                minutes_until = (event_time - reference_time).total_seconds() / 60
                if minutes_until < -60 or minutes_until > 60:
                    continue
                name = event.get("name", "?")
                # If not already included, add it.
                if not any(str(x.get("name")) == str(name) and str(x.get("time")) == event_time.strftime("%H:%M") for x in upcoming):
                    if minutes_until > 0:
                        time_until = f"{int(minutes_until)}m"
                        is_past = False
                    else:
                        time_until = f"{int(abs(minutes_until))}m ago"
                        is_past = True
                    upcoming.append(
                        {
                            "name": name,
                            "time": event_time.strftime("%H:%M"),
                            "importance": importance,
                            "time_until": time_until,
                            "is_past": is_past,
                            "minutes_until": round(minutes_until, 1),
                            "reference_time": reference_time.strftime("%Y-%m-%d %H:%M:%S"),
                            "server_utc_offset_hours": server_utc_offset_hours,
                        }
                    )
        except Exception:
            pass
        
        # Sort: future first (soonest first), then past (most recent first)
        # NOTE: `time_until` is a human string. Always sort using numeric minutes.
        def _sort_key(e: Dict) -> Tuple[int, float]:
            try:
                is_past = bool(e.get("is_past"))
                minutes = float(e.get("minutes_until"))
            except Exception:
                is_past = False
                minutes = 1e9

            # future: small positive first
            if not is_past:
                return (0, minutes)

            # past: most recent first => minutes is negative, so higher (closer to 0) should come first
            return (1, abs(minutes))

        upcoming.sort(key=_sort_key)
        
        return upcoming[:max_events]
        
    except Exception as e:
        log.debug(f"get_upcoming_events: {e}")
        return []


# ============================================================================
# TEST
# ============================================================================

def test_calendar():
    """Test the economic calendar module"""
    print("=" * 60)
    print("ECONOMIC CALENDAR TEST")
    print("=" * 60)
    
    data = get_calendar_data()
    
    print(f"\nSource: {data['source']}")
    print(f"Phase: {data['phase']} - {data['phase_description']}")
    print(f"Score: {data['score']}/100")
    print(f"Bias: {data['bias']}")
    print(f"Events: {data['events_count']}")
    
    if data['closest_event']:
        ev = data['closest_event']
        print(f"\nClosest event:")
        print(f"  Name: {ev.get('name', '?')}")
        print(f"  Time: {ev.get('time_server', '?')}")
        print(f"  Actual: {ev.get('actual_value', 'N/A')}")
        print(f"  Forecast: {ev.get('forecast_value', 'N/A')}")
        print(f"  Previous: {ev.get('previous_value', 'N/A')}")
    
    if data['events']:
        print(f"\nAll events:")
        for ev in data['events']:
            print(f"  - {ev.get('time_server', '?')} | {ev.get('name', '?')}")
    
    if data['error']:
        print(f"\nError: {data['error']}")
    
    print(f"\n{'=' * 60}")


if __name__ == "__main__":
    test_calendar()
