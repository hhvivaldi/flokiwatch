"""
AGENT MEMORY MODULE
Persistent memory for AI Agent REJECT decisions.
Stores market view, conditions to approve, and invalidation timeframe.
Version 1.3

FALLBACK BEHAVIOR:
- All read/write operations wrapped in try/except
- Never raise exceptions to caller
- Return None or empty context on any failure
- Log all failures at warning level
- Never block the main loop
"""

import json
import os
from datetime import datetime, timedelta
from tz_utils import utc_iso, utc_now, trading_day_utc  # FLO-309
from typing import Dict, Optional, List, Any
from dataclasses import dataclass, field, asdict

from logger import log

MEMORY_FILE = "data/agent_memory.json"
SESSION_MEMORY_FILE = "data/agent_session_memory.json"
SCHEMA_VERSION = 1
MAX_HISTORY_ENTRIES = 20


@dataclass
class MarketView:
    """Agent's view of the market at time of REJECT"""
    direction: str  # "BUY", "SELL", "HOLD"
    description: str  # Agent's reasoning for its view


@dataclass
class Condition:
    """A specific condition the Agent set for approval"""
    id: str
    description: str  # Human-readable condition
    indicator: Optional[str] = None  # Technical indicator name (for automated checking)
    operator: Optional[str] = None  # "gt", "gte", "lt", "lte", "between", "eq"
    values: Optional[List[float]] = None  # Threshold values
    met: bool = False  # Whether condition has been met
    current_value: Optional[float] = None  # Current indicator value (updated each cycle)


@dataclass
class Invalidation:
    """When the REJECT context expires"""
    type: str  # "candles" or "time"
    timeframe: str  # "H1", "M5", etc.
    count: int  # Number of candles
    expires_at: str  # ISO timestamp


@dataclass
class ActiveReject:
    """Currently active REJECT context"""
    timestamp: str  # When the REJECT was issued
    brain_signal: str  # What the Brain recommended (BUY/SELL)
    brain_score: float  # Brain's score at time of REJECT
    market_view: MarketView
    conditions_to_approve: List[Condition]
    invalidation: Invalidation
    status: str = "active"  # "active", "conditions_met", "invalidated", "superseded"

    def to_dict(self) -> Dict:
        """Convert to dictionary for JSON serialization"""
        return {
            "timestamp": self.timestamp,
            "brain_signal": self.brain_signal,
            "brain_score": self.brain_score,
            "market_view": {
                "direction": self.market_view.direction,
                "description": self.market_view.description,
            },
            "conditions_to_approve": [
                {
                    "id": c.id,
                    "description": c.description,
                    "indicator": c.indicator,
                    "operator": c.operator,
                    "values": c.values,
                    "met": c.met,
                    "current_value": c.current_value,
                }
                for c in self.conditions_to_approve
            ],
            "invalidation": {
                "type": self.invalidation.type,
                "timeframe": self.invalidation.timeframe,
                "count": self.invalidation.count,
                "expires_at": self.invalidation.expires_at,
            },
            "status": self.status,
        }


@dataclass
class AgentMemory:
    """Complete agent memory state"""
    version: int = SCHEMA_VERSION
    last_updated: str = ""
    active_reject: Optional[ActiveReject] = None
    history: List[Dict] = field(default_factory=list)


def _atomic_write_json(path: str, payload: Dict[str, Any]) -> bool:
    """Write JSON atomically. Returns True on success, False on failure."""
    try:
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)

        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp_path, path)
        return True
    except Exception as e:
        log.warning(f"agent_memory: write failed: {e}")
        return False


def write_sage_insights(recommendations: List[Any], trade_count: int, report_date: str) -> bool:
    """Write Sage recommendations into session memory as a single protected note.

    Behavior (non-blocking):
    - Load data/agent_session_memory.json (create if missing)
    - Remove existing notes with source == "sage"
    - Append ONE combined note with {time, note, source:"sage"}
    - Write back atomically

    Returns True on success, False on failure. Never raises.
    """
    try:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        data_dir = os.path.join(base_dir, "data")
        mem_path = os.path.join(data_dir, os.path.basename(SESSION_MEMORY_FILE))
        os.makedirs(data_dir, exist_ok=True)

        # FLO-309: session boundary was local midnight; now trading_day_utc
        # (UTC midnight) to match the rest of the project's day calculations.
        # Side effect: session reset shifts by +2h for CEST users — intentional.
        today = trading_day_utc()
        payload: Dict[str, Any] = {
            "session_date": today,
            "thesis": "",
            "trades_today": 0,
            "wins_today": 0,
            "losses_today": 0,
            "notes": [],
            "last_updated": now.isoformat(timespec="seconds"),
        }

        if os.path.exists(mem_path):
            try:
                with open(mem_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
                if isinstance(existing, dict):
                    payload.update(existing)
            except Exception:
                pass

        if str(payload.get("session_date") or "") != today:
            payload["session_date"] = today
            payload["notes"] = []

        if not isinstance(payload.get("notes"), list):
            payload["notes"] = []

        # Remove prior Sage notes
        try:
            cleaned = []
            for n in payload.get("notes") or []:
                if not isinstance(n, dict):
                    cleaned.append(n)
                    continue
                if str(n.get("source") or "").strip().lower() == "sage":
                    continue
                cleaned.append(n)
            payload["notes"] = cleaned
        except Exception:
            pass

        # Combine recommendations into a single concise note
        lines: List[str] = []
        for it in recommendations if isinstance(recommendations, list) else []:
            if it is None:
                continue
            if isinstance(it, str):
                s = it.strip()
            elif isinstance(it, dict):
                s = str(it.get("recommendation") or it.get("text") or "").strip()
            else:
                s = str(it).strip()
            if s:
                lines.append(f"- {s}")
            if len(lines) >= 5:
                break

        report_date_text = str(report_date or today).strip() or today
        note_text = (
            f"SAGE DAILY BRIEFING ({int(trade_count)} trades, {report_date_text}):\n"
            + ("\n".join(lines) if lines else "- (no recommendations)")
        )

        payload["notes"].append(
            {"time": now.strftime("%H:%M"), "note": note_text, "source": "sage"}
        )

        payload["last_updated"] = now.isoformat(timespec="seconds")

        return _atomic_write_json(mem_path, payload)
    except Exception as e:
        log.warning(f"agent_memory: failed to write sage insights: {e}")
        return False


def _validate_schema(data: Dict) -> bool:
    """Validate memory file schema. Returns True if valid."""
    try:
        if not isinstance(data, dict):
            return False
        if data.get("version") != SCHEMA_VERSION:
            return False
        return True
    except Exception:
        return False


def read_memory() -> Optional[AgentMemory]:
    """
    Read agent memory from file.
    
    Returns:
        AgentMemory object if successful, None on any failure.
        Never raises exceptions.
    """
    try:
        if not os.path.exists(MEMORY_FILE):
            log.info("agent_memory: file not found, starting fresh")
            return None

        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not _validate_schema(data):
            log.warning("agent_memory: schema validation failed, starting fresh")
            return None

        # Parse active_reject if present
        active_reject = None
        ar_data = data.get("active_reject")
        if ar_data:
            try:
                market_view = MarketView(
                    direction=ar_data["market_view"]["direction"],
                    description=ar_data["market_view"]["description"],
                )
                conditions = [
                    Condition(
                        id=c["id"],
                        description=c["description"],
                        indicator=c.get("indicator"),
                        operator=c.get("operator"),
                        values=c.get("values"),
                        met=c.get("met", False),
                        current_value=c.get("current_value"),
                    )
                    for c in ar_data.get("conditions_to_approve", [])
                ]
                invalidation = Invalidation(
                    type=ar_data["invalidation"]["type"],
                    timeframe=ar_data["invalidation"]["timeframe"],
                    count=ar_data["invalidation"]["count"],
                    expires_at=ar_data["invalidation"]["expires_at"],
                )
                active_reject = ActiveReject(
                    timestamp=ar_data["timestamp"],
                    brain_signal=ar_data["brain_signal"],
                    brain_score=ar_data["brain_score"],
                    market_view=market_view,
                    conditions_to_approve=conditions,
                    invalidation=invalidation,
                    status=ar_data.get("status", "active"),
                )
            except (KeyError, TypeError) as e:
                log.warning(f"agent_memory: failed to parse active_reject: {e}")
                active_reject = None

        return AgentMemory(
            version=data.get("version", SCHEMA_VERSION),
            last_updated=data.get("last_updated", ""),
            active_reject=active_reject,
            history=data.get("history", []),
        )

    except json.JSONDecodeError as e:
        log.warning(f"agent_memory: invalid JSON, starting fresh: {e}")
        return None
    except Exception as e:
        log.warning(f"agent_memory: read failed, starting fresh: {e}")
        return None


def write_memory(memory: AgentMemory) -> bool:
    """
    Write agent memory to file.
    
    Args:
        memory: AgentMemory object to write
        
    Returns:
        True on success, False on failure.
        Never raises exceptions.
    """
    try:
        memory.last_updated = utc_iso()  # FLO-309
        
        payload = {
            "version": memory.version,
            "last_updated": memory.last_updated,
            "active_reject": memory.active_reject.to_dict() if memory.active_reject else None,
            "history": memory.history[-MAX_HISTORY_ENTRIES:],  # Trim to max entries
        }
        
        return _atomic_write_json(MEMORY_FILE, payload)
        
    except Exception as e:
        log.warning(f"agent_memory: write failed: {e}")
        return False


def check_invalidation(memory: AgentMemory) -> bool:
    """
    Check if active REJECT has expired.
    
    Args:
        memory: AgentMemory object
        
    Returns:
        True if expired (should clear), False if still valid.
    """
    if not memory or not memory.active_reject:
        return False
    
    try:
        expires_at = datetime.fromisoformat(
            memory.active_reject.invalidation.expires_at.replace("Z", "+00:00")
        )
        now = datetime.utcnow().replace(tzinfo=expires_at.tzinfo)
        
        if now >= expires_at:
            log.info("agent_memory: active REJECT has expired (invalidation timeframe passed)")
            return True
        return False
        
    except Exception as e:
        log.warning(f"agent_memory: failed to check invalidation: {e}")
        return False


def clear_active_reject(memory: AgentMemory, reason: str = "invalidated") -> AgentMemory:
    """
    Clear the active REJECT and move it to history.
    
    Args:
        memory: AgentMemory object
        reason: Why it was cleared ("invalidated", "superseded", "conditions_met")
        
    Returns:
        Updated AgentMemory object
    """
    if not memory:
        memory = AgentMemory()
    
    if memory.active_reject:
        # Add to history before clearing
        history_entry = memory.active_reject.to_dict()
        history_entry["cleared_at"] = utc_iso()  # FLO-309
        history_entry["cleared_reason"] = reason
        memory.history.append(history_entry)
        
        # Trim history to max entries
        memory.history = memory.history[-MAX_HISTORY_ENTRIES:]
        
        memory.active_reject = None
        log.info(f"agent_memory: cleared active REJECT (reason: {reason})")
    
    return memory


def save_reject(
    brain_signal: str,
    brain_score: float,
    market_view_direction: str,
    market_view_description: str,
    conditions: List[str],
    invalidation_str: str,
) -> bool:
    """
    Save a new REJECT decision to memory.
    Supersedes any existing active REJECT.
    
    Args:
        brain_signal: What the Brain recommended (BUY/SELL)
        brain_score: Brain's score at time of REJECT
        market_view_direction: Agent's view ("BUY", "SELL", "HOLD")
        market_view_description: Agent's reasoning
        conditions: List of condition strings from Agent
        invalidation_str: Invalidation string (e.g., "3 H1 candles")
        
    Returns:
        True on success, False on failure
    """
    try:
        # Read existing memory
        memory = read_memory() or AgentMemory()
        
        # Clear existing active REJECT if any
        if memory.active_reject:
            memory = clear_active_reject(memory, reason="superseded")
        
        # Parse invalidation string
        invalidation = _parse_invalidation(invalidation_str)
        
        # Create conditions list
        condition_objects = [
            Condition(
                id=f"cond_{i+1}",
                description=cond,
                met=False,
            )
            for i, cond in enumerate(conditions)
        ]
        
        # Create new active REJECT
        memory.active_reject = ActiveReject(
            timestamp=utc_iso(),  # FLO-309
            brain_signal=brain_signal,
            brain_score=brain_score,
            market_view=MarketView(
                direction=market_view_direction,
                description=market_view_description,
            ),
            conditions_to_approve=condition_objects,
            invalidation=invalidation,
            status="active",
        )
        
        # Write to file
        success = write_memory(memory)
        if success:
            log.info(f"agent_memory: saved new REJECT (view: {market_view_direction}, {len(conditions)} conditions)")
        return success
        
    except Exception as e:
        log.warning(f"agent_memory: failed to save REJECT: {e}")
        return False


def _parse_invalidation(invalidation_str: str) -> Invalidation:
    """
    Parse invalidation string like "3 H1 candles" into Invalidation object.
    
    Args:
        invalidation_str: String like "3 H1 candles"
        
    Returns:
        Invalidation object with computed expires_at
    """
    # Default values
    count = 3
    timeframe = "H1"
    
    try:
        parts = invalidation_str.lower().split()
        if len(parts) >= 2:
            count = int(parts[0])
            timeframe = parts[1].upper()
    except Exception:
        pass
    
    # Calculate expiration time
    timeframe_minutes = {
        "M1": 1,
        "M5": 5,
        "M15": 15,
        "M30": 30,
        "H1": 60,
        "H4": 240,
        "D1": 1440,
    }
    minutes = timeframe_minutes.get(timeframe, 60) * count
    # FLO-309: utc_now() is aware; to_utc_iso normalizes and stamps Z.
    from tz_utils import to_utc_iso
    expires_at = utc_now() + timedelta(minutes=minutes)
    return Invalidation(
        type="candles",
        timeframe=timeframe,
        count=count,
        expires_at=to_utc_iso(expires_at),
    )


def update_condition_status(
    memory: AgentMemory,
    current_indicators: Dict[str, float],
) -> AgentMemory:
    """
    Update condition met status based on current indicator values.
    This is informational only — Agent makes final decision.
    
    Args:
        memory: AgentMemory object
        current_indicators: Dict of indicator name -> current value
        
    Returns:
        Updated AgentMemory object
    """
    if not memory or not memory.active_reject:
        return memory
    
    try:
        for condition in memory.active_reject.conditions_to_approve:
            # Update current value if we have the indicator
            if condition.indicator and condition.indicator in current_indicators:
                condition.current_value = current_indicators[condition.indicator]
                
                # Check if condition is met (informational)
                if condition.operator and condition.values:
                    condition.met = _check_condition(
                        condition.current_value,
                        condition.operator,
                        condition.values,
                    )
        
        # Check if all conditions are met
        all_met = all(c.met for c in memory.active_reject.conditions_to_approve)
        if all_met and memory.active_reject.status == "active":
            memory.active_reject.status = "conditions_met"
            log.info("agent_memory: all conditions now met")
            
    except Exception as e:
        log.warning(f"agent_memory: failed to update condition status: {e}")
    
    return memory


def _check_condition(value: float, operator: str, thresholds: List[float]) -> bool:
    """Check if a value meets a condition."""
    try:
        if operator == "gt":
            return value > thresholds[0]
        elif operator == "gte":
            return value >= thresholds[0]
        elif operator == "lt":
            return value < thresholds[0]
        elif operator == "lte":
            return value <= thresholds[0]
        elif operator == "between":
            return thresholds[0] <= value <= thresholds[1]
        elif operator == "eq":
            return value == thresholds[0]
    except Exception:
        pass
    return False


def get_memory_context_for_agent(current_indicators: Optional[Dict[str, float]] = None) -> Optional[Dict]:
    """
    Get memory context to inject into Agent's data package.
    
    Args:
        current_indicators: Optional dict of current indicator values
        
    Returns:
        Dict with previous REJECT context, or None if no active memory
    """
    try:
        memory = read_memory()
        if not memory:
            return None
        
        # Check if expired
        if check_invalidation(memory):
            memory = clear_active_reject(memory, reason="invalidated")
            write_memory(memory)
            return None
        
        if not memory.active_reject:
            return None
        
        # Update condition status if we have indicators
        if current_indicators:
            memory = update_condition_status(memory, current_indicators)
            write_memory(memory)
        
        ar = memory.active_reject
        
        # Build context for Agent
        conditions_status = []
        for c in ar.conditions_to_approve:
            status = "✅ MET" if c.met else "❌ NOT MET"
            current = f" (current: {c.current_value})" if c.current_value is not None else ""
            conditions_status.append(f"{status}: {c.description}{current}")
        
        all_met = all(c.met for c in ar.conditions_to_approve)
        
        # Calculate time remaining
        try:
            expires_at = datetime.fromisoformat(ar.invalidation.expires_at.replace("Z", "+00:00"))
            now = datetime.utcnow().replace(tzinfo=expires_at.tzinfo)
            remaining = expires_at - now
            remaining_str = f"{int(remaining.total_seconds() // 60)} minutes"
        except Exception:
            remaining_str = "unknown"
        
        return {
            "has_previous_reject": True,
            "reject_timestamp": ar.timestamp,
            "brain_signal_rejected": ar.brain_signal,
            "brain_score_at_reject": ar.brain_score,
            "your_market_view": {
                "direction": ar.market_view.direction,
                "description": ar.market_view.description,
            },
            "conditions_status": conditions_status,
            "all_conditions_met": all_met,
            "invalidation": {
                "timeframe": ar.invalidation.timeframe,
                "candles": ar.invalidation.count,
                "expires_at": ar.invalidation.expires_at,
                "time_remaining": remaining_str,
            },
            "status": ar.status,
        }
        
    except Exception as e:
        log.warning(f"agent_memory: failed to get context: {e}")
        return None


def get_memory_for_dashboard() -> Optional[Dict]:
    """
    Get memory state for dashboard display.
    
    Returns:
        Dict with memory state for dashboard, or None if no active memory
    """
    try:
        memory = read_memory()
        if not memory or not memory.active_reject:
            return None
        
        ar = memory.active_reject
        
        # Calculate time remaining
        try:
            expires_at = datetime.fromisoformat(ar.invalidation.expires_at.replace("Z", "+00:00"))
            now = datetime.utcnow().replace(tzinfo=expires_at.tzinfo)
            remaining = expires_at - now
            remaining_minutes = int(remaining.total_seconds() // 60)
            candles_remaining = max(0, remaining_minutes // {
                "M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440
            }.get(ar.invalidation.timeframe, 60))
        except Exception:
            remaining_minutes = 0
            candles_remaining = 0
        
        # Determine status - EXPIRED if candles_remaining is 0
        display_status = ar.status
        if candles_remaining <= 0:
            display_status = "EXPIRED"
        
        return {
            "timestamp": ar.timestamp,
            "brain_signal": ar.brain_signal,
            "brain_score": ar.brain_score,
            "market_view": {
                "direction": ar.market_view.direction,
                "description": ar.market_view.description,
            },
            "conditions": [
                {
                    "description": c.description,
                    "met": c.met,
                    "current_value": c.current_value,
                }
                for c in ar.conditions_to_approve
            ],
            "invalidation": {
                "timeframe": ar.invalidation.timeframe,
                "candles_total": ar.invalidation.count,
                "candles_remaining": candles_remaining,
                "expires_at": ar.invalidation.expires_at,
            },
            "status": display_status,
            "all_conditions_met": all(c.met for c in ar.conditions_to_approve),
        }
        
    except Exception as e:
        log.warning(f"agent_memory: failed to get dashboard data: {e}")
        return None


# =============================================================================
# TESTS
# =============================================================================

if __name__ == "__main__":
    print("=" * 60)
    print("🧠 AGENT MEMORY MODULE TEST")
    print("=" * 60)
    
    # Test 1: Read non-existent file
    print("\n1. Test read non-existent file:")
    memory = read_memory()
    print(f"   Result: {memory}")
    
    # Test 2: Save a REJECT
    print("\n2. Test save REJECT:")
    success = save_reject(
        brain_signal="BUY",
        brain_score=68.2,
        market_view_direction="SELL",
        market_view_description="I see this as a SELL setup. Price rejected from 2920 resistance.",
        conditions=[
            "RSI pulls back to 45-50 range",
            "Price holds above 2910 on H1 close",
            "Volume ratio exceeds 1.0",
        ],
        invalidation_str="3 H1 candles",
    )
    print(f"   Success: {success}")
    
    # Test 3: Read back
    print("\n3. Test read back:")
    memory = read_memory()
    if memory and memory.active_reject:
        print(f"   Brain signal: {memory.active_reject.brain_signal}")
        print(f"   Market view: {memory.active_reject.market_view.direction}")
        print(f"   Conditions: {len(memory.active_reject.conditions_to_approve)}")
        print(f"   Expires: {memory.active_reject.invalidation.expires_at}")
    
    # Test 4: Get context for Agent
    print("\n4. Test get context for Agent:")
    context = get_memory_context_for_agent()
    if context:
        print(f"   Has previous reject: {context['has_previous_reject']}")
        print(f"   All conditions met: {context['all_conditions_met']}")
        print(f"   Conditions status:")
        for cs in context['conditions_status']:
            print(f"      {cs}")
    
    # Test 5: Get dashboard data
    print("\n5. Test get dashboard data:")
    dashboard = get_memory_for_dashboard()
    if dashboard:
        print(f"   Market view: {dashboard['market_view']['direction']}")
        print(f"   Candles remaining: {dashboard['invalidation']['candles_remaining']}")
    
    print("\n" + "=" * 60)
    print("✅ Tests complete")
