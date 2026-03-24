"""
AI AGENT - Claude-based Trading Decision Maker
The Agent receives market data, Brain analysis, and makes independent trading decisions.
Agent is the decision maker and executor.
"""

import json
import asyncio
import os
import re
import time
import copy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional, List, Any
from enum import Enum

from logger import log
from agent_prompts import get_system_prompt, get_prompt_hash, get_prompt_version

logger = log


def _update_session_memory(session_notes: str, session_context: Optional[Dict[str, Any]] = None) -> None:
    try:
        notes_s = str(session_notes or "").strip()
        if not notes_s:
            return

        base_dir = os.path.dirname(os.path.abspath(__file__))
        data_dir = os.path.join(base_dir, "data")
        mem_path = os.path.join(data_dir, "agent_session_memory.json")
        os.makedirs(data_dir, exist_ok=True)

        now = datetime.now()
        today = now.date().isoformat()

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

        # Daily cutoff: if session_date != today, clear notes so stale context doesn't persist.
        if str(payload.get("session_date") or "") != today:
            preserved_sage_notes = []
            try:
                for n in payload.get("notes") or []:
                    if isinstance(n, dict) and str(n.get("source") or "").strip().lower() == "sage":
                        preserved_sage_notes.append(n)
            except Exception:
                preserved_sage_notes = []
            try:
                prev_date = str(payload.get("session_date") or "").strip()
                if prev_date:
                    archive_path = os.path.join(data_dir, f"agent_session_memory_{prev_date}.json")
                    try:
                        if os.path.exists(mem_path) and not os.path.exists(archive_path):
                            os.replace(mem_path, archive_path)
                    except Exception:
                        pass
            except Exception:
                pass

            payload = {
                "session_date": today,
                "thesis": "",
                "trades_today": 0,
                "wins_today": 0,
                "losses_today": 0,
                "notes": preserved_sage_notes,
                "last_updated": now.isoformat(timespec="seconds"),
            }

        sc = session_context if isinstance(session_context, dict) else {}
        try:
            payload["trades_today"] = int(sc.get("today_trades", payload.get("trades_today", 0)) or 0)
            payload["wins_today"] = int(sc.get("today_wins", payload.get("wins_today", 0)) or 0)
            payload["losses_today"] = int(sc.get("today_losses", payload.get("losses_today", 0)) or 0)
        except Exception:
            pass

        if not isinstance(payload.get("notes"), list):
            payload["notes"] = []

        payload["notes"].append({"time": now.strftime("%H:%M"), "note": notes_s})

        # Keep max 20 notes, protect Sage notes from truncation.
        # Strategy: keep all notes where source == 'sage', truncate only non-sage notes to last 19.
        try:
            all_notes = payload.get("notes") or []
            sage_notes = []
            normal_notes = []
            for n in all_notes:
                if isinstance(n, dict) and str(n.get("source") or "").strip().lower() == "sage":
                    sage_notes.append(n)
                else:
                    normal_notes.append(n)
            normal_notes = normal_notes[-19:]
            payload["notes"] = normal_notes + sage_notes
            payload["notes"] = payload["notes"][-20:]
        except Exception:
            payload["notes"] = payload["notes"][-20:]
        payload["last_updated"] = now.isoformat(timespec="seconds")

        try:
            with open(mem_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
    except Exception as e:
        logger.debug(f"Session memory update failed (non-blocking): {e}")


def _first_number(text: Any) -> Optional[float]:
    try:
        s = str(text or "")
        m = re.search(r"-?\d+(?:\.\d+)?", s)
        if not m:
            return None
        return float(m.group(0))
    except Exception:
        return None


def _get_data_value(data_package: Dict[str, Any], key: str) -> Optional[float]:
    try:
        dp = data_package or {}
        ind = dp.get("indicators") or {}
        macro = dp.get("macro") or {}

        if key == "rsi":
            return float(((ind.get("rsi") or {}).get("value")))
        if key == "ema200":
            emas = ind.get("emas") or {}
            return float(emas.get("ema200"))
        if key == "atr":
            atr = ind.get("atr") or {}
            return float(atr.get("value"))
        if key == "dxy":
            dxy = macro.get("dxy") or {}
            return float(dxy.get("value"))
        if key == "vix":
            vix = macro.get("vix") or {}
            return float(vix.get("value"))
        return None
    except Exception:
        return None


def _validate_checklist(parsed: Dict[str, Any], data_package: Dict[str, Any]) -> Dict[str, Any]:
    validation: Dict[str, Any] = {
        "has_checklist": False,
        "missing_fields": [],
        "mismatches": {},
        "reasoning_categories_count": 0,
        "reasoning_categories": [],
    }

    checklist = parsed.get("data_checklist")
    if not isinstance(checklist, dict):
        logger.warning("AGENT_CHECKLIST | MISSING — agent did not include data_checklist")
        return validation

    validation["has_checklist"] = True

    required_fields = [
        "price_action",
        "ema200",
        "rsi",
        "macd",
        "fibonacci",
        "atr",
        "macro",
        "headlines_summary",
        "calendar",
        "sr_zones",
        "volume",
        "mtf_trend",
        "session",
    ]

    missing: List[str] = []
    for f in required_fields:
        v = checklist.get(f)
        if v is None:
            missing.append(f)
            continue
        vs = str(v).strip()
        if not vs or vs.lower() in ("n/a", "na", "none", "unknown", "null"):
            missing.append(f)

    if missing:
        logger.warning(f"AGENT_CHECKLIST | INCOMPLETE | missing: {', '.join(missing)}")
    validation["missing_fields"] = missing

    tolerances = {"rsi": 5.0, "ema200": 5.0, "atr": 5.0, "dxy": 1.0, "vix": 2.0}
    mapping = {"rsi": "rsi", "ema200": "ema200", "atr": "atr", "dxy": "macro", "vix": "macro"}

    mismatches: Dict[str, Any] = {}
    for k, tol in tolerances.items():
        agent_field = mapping.get(k)
        agent_val = _first_number(checklist.get(agent_field))
        data_val = _get_data_value(data_package, k)
        if agent_val is None or data_val is None:
            continue
        if abs(agent_val - data_val) > tol:
            mismatches[k] = {"agent": agent_val, "data": data_val, "tolerance": tol}
            logger.warning(f"AGENT_CHECKLIST | MISMATCH | {k}: agent={agent_val} data={data_val}")

    validation["mismatches"] = mismatches

    reasoning = str(parsed.get("reasoning") or "")
    category_keywords = {
        "price_action": ["price", "structure", "higher", "lower", "resistance", "support"],
        "ema200": ["ema", "ema200"],
        "rsi": ["rsi"],
        "macd": ["macd"],
        "fibonacci": ["fib", "fibonacci", "61.8", "50%", "38.2", "23.6"],
        "atr": ["atr"],
        "macro": ["dxy", "vix", "yield", "yields", "10y"],
        "headlines_summary": ["reuters", "headline", "iran", "tariff", "fed"],
        "calendar": ["calendar", "event", "cpi", "nfp", "fomc", "pce"],
        "sr_zones": ["sr", "zone", "touch", "support", "resistance"],
        "volume": ["volume", "tick"],
        "mtf_trend": ["d1", "h4", "mtf"],
        "session": ["session", "asian", "london", "ny"],
    }

    referenced: List[str] = []
    r_low = reasoning.lower()
    for cat, kws in category_keywords.items():
        if any(kw in r_low for kw in kws):
            referenced.append(cat)

    validation["reasoning_categories"] = referenced
    validation["reasoning_categories_count"] = len(referenced)
    if len(referenced) < 8:
        logger.warning(f"AGENT_CHECKLIST | SHALLOW | only {len(referenced)}/13 categories referenced in reasoning")

    return validation


class AgentDecision(Enum):
    """Possible Agent decisions"""
    OPEN_BUY = "OPEN_BUY"
    OPEN_SELL = "OPEN_SELL"
    HOLD_TRADE = "HOLD_TRADE"
    ADJUST_TRADE = "ADJUST_TRADE"
    CLOSE_TRADE = "CLOSE_TRADE"
    REJECT = "REJECT"
    WAIT = "WAIT"
    DEFER_TO_BRAIN = "DEFER_TO_BRAIN"  # Fallback when Agent fails


@dataclass
class AgentResult:
    """Result from the AI Agent"""
    decision: str
    confidence: int
    reasoning: str
    key_factors: List[str]
    concerns: List[str]
    trade_plan: Optional[Dict[str, Any]] = None
    entry_conditions: Optional[Dict[str, Any]] = None
    session_notes: Optional[str] = None
    checklist_validation: Optional[Dict[str, Any]] = None
    raw_response: Optional[str] = None
    prompt_version: str = ""
    prompt_hash: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    error: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.utcnow)
    market_view: Optional[Dict] = None
    conditions_to_approve: Optional[List[str]] = None
    invalidation: Optional[str] = None
    adjustment: Optional[Dict] = None
    close_reason: Optional[str] = None

    def to_dict(self) -> Dict:
        """Convert to dictionary for logging/storage"""
        result = {
            "decision": self.decision,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "key_factors": self.key_factors,
            "concerns": self.concerns,
            "trade_plan": self.trade_plan,
            "entry_conditions": self.entry_conditions,
            "session_notes": self.session_notes,
            "checklist_validation": self.checklist_validation,
            "raw_response": self.raw_response,
            "prompt_version": self.prompt_version,
            "prompt_hash": self.prompt_hash,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "latency_ms": self.latency_ms,
            "error": self.error,
            "timestamp": self.timestamp.isoformat(),
        }
        try:
            tool_trace = getattr(self, "tool_trace", None)
            if tool_trace is not None:
                result["tool_trace"] = tool_trace
        except Exception:
            pass
        if self.market_view:
            result["market_view"] = self.market_view
        if self.conditions_to_approve:
            result["conditions_to_approve"] = self.conditions_to_approve
        if self.invalidation:
            result["invalidation"] = self.invalidation
        if self.adjustment:
            result["adjustment"] = self.adjustment
        if self.close_reason:
            result["close_reason"] = self.close_reason
        return result


class AIAgent:
    """
    AI Agent that makes trading decisions using Claude.
    
    The Agent receives:
    - Raw price data (H1/M5 candles)
    - Technical indicators
    - Brain analysis (as reference)
    - ML predictions
    - News and macro data
    - Open positions
    - Session context
    
    The Agent reasons through the context and decides:
    - OPEN_BUY / OPEN_SELL: Take the trade
    - REJECT: Brain suggested a trade but context is wrong
    - WAIT: Setup forming but not ready
    - DEFER_TO_BRAIN: Fallback when Agent fails
    """

    def __init__(self):
        """Initialize the AI Agent"""
        self.client = None
        self.model = None
        self.timeout = 60
        self.enabled = False
        self.mode = "shadow"  # shadow | gate | full
        self._initialized = False

        self.max_tool_calls = 15
        self.max_tokens = 4096

    def initialize(self) -> bool:
        """
        Initialize the Gemini client.
        Call this after config is loaded.
        
        Returns:
            True if initialization successful, False otherwise
        """
        try:
            import config
            
            # Check if Agent is enabled
            self.enabled = getattr(config, 'USE_AI_AGENT', False)
            if not self.enabled:
                logger.info("AI Agent is disabled in config")
                return False
            
            # Get API key
            api_key = os.environ.get("GEMINI_API_KEY", "")
            if not api_key:
                logger.warning("GEMINI_API_KEY not set - AI Agent disabled")
                self.enabled = False
                return False

            # Import and initialize Gemini client
            try:
                from google import genai

                self.client = genai.Client(api_key=api_key)
            except ImportError:
                logger.error("google-genai package not installed. Run: pip install google-genai")
                self.enabled = False
                return False
            except Exception as e:
                logger.error(f"Failed to initialize Gemini client: {e}")
                self.enabled = False
                return False
            
            # Get config
            self.model = getattr(config, 'FLOKI_MODEL', 'gemini-3-flash-preview')
            self.timeout = getattr(config, 'AI_AGENT_TIMEOUT', 60)
            self.mode = getattr(config, 'AI_AGENT_MODE', 'shadow')

            self.max_tool_calls = int(getattr(config, 'AI_AGENT_MAX_TOOL_CALLS', 15) or 15)
            self.max_tokens = int(getattr(config, 'AI_AGENT_MAX_TOKENS', 4096) or 4096)
            
            self._initialized = True
            logger.info(f"AI Agent initialized: model={self.model}, mode={self.mode}, timeout={self.timeout}s")
            return True
            
        except Exception as e:
            logger.error(f"Failed to initialize AI Agent: {e}")
            self.enabled = False
            return False

    async def decide(self, trigger_context: Any, tools: Any, trigger_type: str = "SIGNAL") -> AgentResult:
        """
        Make a trading decision based on minimal trigger context.

        Args:
            trigger_context: Short message explaining why the Agent was called
            tools: AgentTools instance (agent_tools.AgentTools)

        Returns:
            AgentResult with decision and reasoning
        """
        if not self.enabled or not self._initialized:
            return self._fallback_result("Agent not enabled or not initialized")
        
        start_time = datetime.utcnow()
        
        try:
            user_message = str(trigger_context or "").strip()
            if not user_message:
                user_message = "Scheduled analysis. Decide what to check and whether to act."

            response = await asyncio.wait_for(
                self._call_gemini_with_tools(user_message, tools=tools),
                timeout=self.timeout,
            )
            
            # Calculate latency
            latency_ms = int((datetime.utcnow() - start_time).total_seconds() * 1000)
            
            result = await self._parse_response_with_retry(response, latency_ms, user_message)

            try:
                tool_trace = response.get("tool_trace")
                if tool_trace is not None:
                    try:
                        setattr(result, "tool_trace", tool_trace)
                    except Exception:
                        pass
            except Exception:
                pass
            
            logger.info(
                f"Agent decision: {result.decision} (conf={result.confidence}) "
                f"[{result.input_tokens}+{result.output_tokens} tokens, {latency_ms}ms]"
            )
            
            return result
            
        except asyncio.TimeoutError:
            latency_ms = int((datetime.utcnow() - start_time).total_seconds() * 1000)
            logger.warning(f"Agent timeout after {latency_ms}ms - deferring to Brain")
            return self._fallback_result(f"Timeout after {latency_ms}ms")
            
        except Exception as e:
            latency_ms = int((datetime.utcnow() - start_time).total_seconds() * 1000)
            logger.error(f"Agent error: {e}")
            return self._fallback_result(str(e))

    def _luna_brief_is_fresh(self) -> bool:
        """Check if Luna brief exists, is enabled, and is fresh (< 30 min)."""
        try:
            import config
            if not bool(getattr(config, "LUNA_ENABLED", False)):
                return False
            from luna_analyst import load_luna_brief
            brief = load_luna_brief()
            if not brief or not brief.get("timestamp"):
                return False
            from datetime import datetime, timezone
            brief_time = datetime.fromisoformat(brief["timestamp"].replace("Z", "+00:00"))
            age_min = (datetime.now(timezone.utc) - brief_time).total_seconds() / 60
            if age_min > 30:
                log.info("LUNA | Brief stale — Floki using raw macro tools (fallback)")
                return False
            return True
        except Exception:
            return False

    def _macro_tools_if_needed(self) -> List[Dict[str, Any]]:
        """Return get_headlines + get_macro tools only if Luna brief is NOT fresh."""
        if self._luna_brief_is_fresh():
            return []
        return [
            {
                "name": "get_headlines",
                "description": "Get cached news headlines (max 10)",
                "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "get_macro",
                "description": "Get cached macro snapshot (DXY, VIX, yields, etc.)",
                "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        ]

    def _tool_schemas(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "get_current_price",
                "description": "Get current bid/ask/spread from cached Brain data",
                "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "get_position_events",
                "description": "Get recent position-management events from the Monitor (breakeven, trailing, forced closes)",
                "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "get_candles",
                "description": "Get cached OHLCV candles for a timeframe. Supported: M5, H1, H4, D1. Max count: 50.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "timeframe": {"type": "string"},
                        "count": {"type": "integer", "minimum": 1, "maximum": 50},
                    },
                    "required": ["timeframe", "count"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "get_indicators",
                "description": "Get cached indicator snapshot (RSI, MACD, EMA200, ATR, ADX, Bollinger)",
                "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "get_sr_zones",
                "description": "Get cached support/resistance zones",
                "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "get_fibonacci_levels",
                "description": "Get cached Fibonacci levels and swing high/low",
                "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            # get_headlines + get_macro: conditionally excluded when Luna is active
            *self._macro_tools_if_needed(),
            {
                "name": "get_calendar",
                "description": "Get cached economic calendar phase + next event",
                "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "get_ml_prediction",
                "description": "Get cached ML prediction snapshot",
                "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "get_open_positions",
                "description": "Get open positions from execution layer",
                "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "get_trade_history",
                "description": "Get recent closed trade history (days=1..30)",
                "input_schema": {
                    "type": "object",
                    "properties": {"days": {"type": "integer", "minimum": 1, "maximum": 30}},
                    "required": ["days"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "get_account_info",
                "description": "Get account balance/equity/margin/leverage",
                "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "execute_trade",
                "description": "Execute a trade (action). Safety is enforced and may reject.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "direction": {"type": "string"},
                        "sl": {"type": "number"},
                        "tp": {"type": "number"},
                        "agent_confidence": {"type": "number"},
                    },
                    "required": ["direction", "sl", "tp"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "debate_with_rex",
                "description": "Debate with Rex (junior trader) for a second perspective. Max 5 turns per decision (auto-resets after 5 minutes).",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "my_direction": {"type": "string"},
                        "my_reasoning": {"type": "string"},
                        "my_confidence": {"type": "number"},
                        "key_data": {},
                        "rex_previous_response": {},
                    },
                    "required": ["my_direction", "my_reasoning", "my_confidence", "key_data"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "set_watch_conditions",
                "description": "After opening a trade, set watch conditions for Simba to monitor the position. Supported types: price_touch (level + optional tolerance fields — triggers when price reaches level), pnl_threshold (value in dollars — negative for loss alert e.g. -15, positive for profit alert e.g. 20). Example: {type: 'pnl_threshold', value: -15, description: 'Max acceptable loss'}.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "ticket": {"type": "integer"},
                        "conditions": {
                            "type": "array",
                            "items": {"type": "object"},
                        },
                    },
                    "required": ["ticket", "conditions"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "set_wake_conditions",
                "description": "When deciding WAIT with no open position, set wake conditions for Simba to monitor (every 30s) and wake you when conditions are met. Supported types: price_above/price_below (level field), rsi_above/rsi_below (value field, H1 RSI), volume_above (value field, H1 tick volume), adx_above (value field, H1 ADX), scanner_pattern (pattern field — e.g. 'engulfing', 'pin_bar', 'doji', 'hammer'), indicator_above/indicator_below (indicator + threshold fields — works for any cached indicator like 'macd', 'ema_9', 'atr'). Optional 'group' field: conditions in same group use AND logic (all must be met). Different groups or ungrouped conditions use OR. Example: {type: 'rsi_above', value: 70, group: 'A'} + {type: 'volume_above', value: 15000, group: 'A'} = wake when RSI > 70 AND volume > 15K.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "max_sleep_minutes": {"type": "integer"},
                        "conditions": {
                            "type": "array",
                            "items": {"type": "object"},
                        },
                    },
                    "required": ["max_sleep_minutes", "conditions"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "close_trade",
                "description": "Close a trade by ticket",
                "input_schema": {
                    "type": "object",
                    "properties": {"ticket": {"type": "integer"}},
                    "required": ["ticket"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "adjust_trade",
                "description": "Adjust SL/TP of an open trade",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "ticket": {"type": "integer"},
                        "new_sl": {"type": "number"},
                        "new_tp": {"type": "number"},
                    },
                    "required": ["ticket", "new_sl", "new_tp"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "read_session_memory",
                "description": "Read session memory notes (trading journal)",
                "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "get_trade_patterns",
                "description": "Read discovered statistical patterns from your own trading history (L2 warm memory).",
                "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "get_trade_lessons",
                "description": "Read dynamic lessons from your past trades. Shows AVOID patterns (setups that keep losing) and PREFERRED patterns (setups that keep winning). Call BEFORE opening any trade.",
                "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "write_session_memory",
                "description": "Write session memory (thesis + note)",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "thesis": {"type": "string"},
                        "note": {"type": "string"},
                    },
                    "required": ["thesis", "note"],
                    "additionalProperties": False,
                },
                "cache_control": {"type": "ephemeral"},
            },
            {
                "name": "set_next_check",
                "description": "Schedule your next analysis cycle. Writes next_check_at timestamp; bounds: 2-120 minutes (default 5).",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "minutes": {"type": "integer", "minimum": 2, "maximum": 120},
                    },
                    "required": ["minutes"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "get_echo_alerts",
                "description": "Get pending news alerts from Echo News Sentinel (IMPORTANT/CRITICAL headlines). Marks alerts as read after retrieval.",
                "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "get_luna_brief",
                "description": "Get the latest Luna macro analysis brief (environment, risk level, directional bias, patterns, market regime). Returns stale=true if brief is older than 30 minutes.",
                "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        ]

    def _gemini_function_declarations(self) -> List[Dict[str, Any]]:
        def _strip_additional_props(obj: Any) -> Any:
            if isinstance(obj, dict):
                if "additionalProperties" in obj:
                    try:
                        obj.pop("additionalProperties", None)
                    except Exception:
                        pass
                if "additional_properties" in obj:
                    try:
                        obj.pop("additional_properties", None)
                    except Exception:
                        pass
                for k, v in list(obj.items()):
                    obj[k] = _strip_additional_props(v)
                return obj
            if isinstance(obj, list):
                return [_strip_additional_props(x) for x in obj]
            return obj

        decls: List[Dict[str, Any]] = []
        for t in self._tool_schemas():
            try:
                name = t.get("name")
                desc = t.get("description")
                schema = t.get("input_schema")
                if not name or not isinstance(schema, dict):
                    continue
                schema_copy = copy.deepcopy(schema)
                schema_copy = _strip_additional_props(schema_copy)
                decls.append(
                    {
                        "name": name,
                        "description": desc or "",
                        "parameters": schema_copy,
                    }
                )
            except Exception:
                continue
        return decls

    def _execute_tool(self, tools: Any, name: str, tool_input: Dict[str, Any]) -> Dict[str, Any]:
        try:
            fn = getattr(tools, name, None)
            if not callable(fn):
                return {"success": False, "reason": f"unknown tool: {name}"}
            if tool_input is None:
                tool_input = {}
            return fn(**tool_input)
        except TypeError as e:
            return {"success": False, "reason": f"invalid tool args: {e}"}
        except Exception as e:
            return {"success": False, "reason": f"tool error: {e}"}

    @staticmethod
    def _looks_like_non_json_text(content: Any) -> bool:
        try:
            trimmed = str(content or "").lstrip()
            if not trimmed:
                return False
            return not trimmed.startswith("{") and not trimmed.startswith("[")
        except Exception:
            return False

    async def _call_gemini_with_tools(self, trigger_context: str, tools: Any) -> Dict[str, Any]:
        loop = asyncio.get_event_loop()
        start_time = time.time()

        total_input_tokens = 0
        total_output_tokens = 0
        last_model = self.model
        tool_calls = 0
        tool_trace: List[Dict[str, Any]] = []

        system_prompt = ""
        try:
            system_prompt = get_system_prompt()
        except Exception:
            system_prompt = ""

        fn_decls = self._gemini_function_declarations()

        # Gemini chat content format: list of content items with role + parts.
        contents: List[Dict[str, Any]] = []
        if system_prompt:
            contents.append({"role": "user", "parts": [{"text": system_prompt}]})
        contents.append({"role": "user", "parts": [{"text": str(trigger_context or "").strip()}]})

        had_empty_retry = False
        budget_warning_injected = False

        while True:
            if (time.time() - start_time) >= float(self.timeout):
                return {
                    "content": json.dumps(
                        {
                            "decision": "WAIT",
                            "confidence": 0,
                            "reasoning": "Timeout during tool loop",
                            "key_factors": [],
                            "concerns": ["timeout"],
                        }
                    ),
                    "input_tokens": total_input_tokens,
                    "output_tokens": total_output_tokens,
                    "model": last_model,
                    "tool_trace": tool_trace,
                }

            if tool_calls >= int(self.max_tool_calls):
                return {
                    "content": json.dumps(
                        {
                            "decision": "WAIT",
                            "confidence": 0,
                            "reasoning": "max tool calls reached",
                            "key_factors": [],
                            "concerns": ["max_tool_calls_reached"],
                        }
                    ),
                    "input_tokens": total_input_tokens,
                    "output_tokens": total_output_tokens,
                    "model": last_model,
                    "tool_trace": tool_trace,
                }

            def _sync_call():
                return self.client.models.generate_content(
                    model=self.model,
                    contents=contents,
                    config={
                        "max_output_tokens": int(self.max_tokens),
                        "temperature": 0.2,
                        "tools": [{"function_declarations": fn_decls}],
                    },
                )

            resp = await loop.run_in_executor(None, _sync_call)

            try:
                usage = getattr(resp, "usage_metadata", None)
                if usage is not None:
                    logger.info(
                        f"GEMINI_USAGE | total_tokens={getattr(usage, 'total_token_count', 0)} cached_tokens={getattr(usage, 'cached_content_token_count', 0)} prompt_tokens={getattr(usage, 'prompt_token_count', 0)}"
                    )
            except Exception:
                pass

            finish_reason = None
            try:
                candidates = getattr(resp, "candidates", None) or []
                c0 = candidates[0] if candidates else None
                finish_reason = getattr(c0, "finish_reason", None)
            except Exception:
                finish_reason = None

            try:
                usage = getattr(resp, "usage_metadata", None)
                if usage is not None:
                    total_input_tokens += int(getattr(usage, "prompt_token_count", 0) or 0)
                    total_output_tokens += int(getattr(usage, "candidates_token_count", 0) or 0)
            except Exception:
                pass

            text_out = None
            fn_calls = []
            parts = []
            part_types = []
            try:
                # google-genai returns candidates[0].content.parts
                candidates = getattr(resp, "candidates", None) or []
                c0 = candidates[0] if candidates else None
                content = getattr(c0, "content", None)
                parts = getattr(content, "parts", None) or []
                for p in parts:
                    fc = getattr(p, "function_call", None)
                    if fc is not None:
                        fn_calls.append(fc)
                    else:
                        t = getattr(p, "text", None)
                        if isinstance(t, str) and t.strip():
                            text_out = (text_out or "") + t

                try:
                    for p in parts:
                        try:
                            if getattr(p, "function_call", None) is not None:
                                part_types.append("function_call")
                            elif getattr(p, "text", None) is not None:
                                part_types.append("text")
                            else:
                                part_types.append(type(p).__name__)
                        except Exception:
                            part_types.append("unknown")
                except Exception:
                    part_types = []
            except Exception:
                text_out = None
                fn_calls = []
                parts = []
                part_types = []

            logger.info(
                f"GEMINI_PARTS | types={part_types} fn_calls={len(fn_calls)} text_out={'Y' if (text_out and str(text_out).strip()) else 'N'} finish_reason={finish_reason} tool_calls={tool_calls}/{int(self.max_tool_calls)}"
            )

            if text_out and not fn_calls:
                return {
                    "content": str(text_out).strip(),
                    "input_tokens": total_input_tokens,
                    "output_tokens": total_output_tokens,
                    "model": last_model,
                    "tool_trace": tool_trace,
                }

            if not fn_calls:
                try:
                    fr_s = str(finish_reason or "").strip().upper()
                    had_text_part = any(str(t).strip().lower() == "text" for t in (part_types or []))
                    empty_text_part = had_text_part and not (text_out and str(text_out).strip())
                    retryable_finish = bool(fr_s) and fr_s not in ("STOP", "MAX_TOKENS")
                    if (not had_empty_retry) and (empty_text_part or retryable_finish):
                        had_empty_retry = True
                        tool_trace.append(
                            {
                                "name": "gemini_retry",
                                "input": {
                                    "reason": "empty_text_part" if empty_text_part else "empty_response",
                                    "finish_reason": fr_s,
                                },
                                "result": {"success": True, "retry": 1},
                                "latency_ms": 0,
                            }
                        )
                        continue
                except Exception:
                    pass
                return {
                    "content": json.dumps(
                        {
                            "decision": "WAIT",
                            "confidence": 0,
                            "reasoning": "empty response",
                            "key_factors": [],
                            "concerns": ["empty_response"],
                        }
                    ),
                    "input_tokens": total_input_tokens,
                    "output_tokens": total_output_tokens,
                    "model": last_model,
                    "tool_trace": tool_trace,
                }

            # Execute requested function calls and append function_response parts.
            for fc in fn_calls:
                if tool_calls >= int(self.max_tool_calls):
                    break
                tool_calls += 1
                try:
                    name = str(getattr(fc, "name", "") or "").strip()
                    args = getattr(fc, "args", None)
                    if not isinstance(args, dict):
                        try:
                            args = dict(args) if args is not None else {}
                        except Exception:
                            args = {}

                    t0 = time.time()
                    result = self._execute_tool(tools, name, args)
                    dt_ms = int((time.time() - t0) * 1000)

                    tool_trace.append({"name": name, "input": args, "result": result, "latency_ms": dt_ms})

                    contents.append(
                        {
                            "role": "user",
                            "parts": [
                                {
                                    "function_response": {
                                        "name": name,
                                        "response": result,
                                    }
                                }
                            ],
                        }
                    )

                    _budget_warn_at = int(self.max_tool_calls) - 5
                    if (not budget_warning_injected) and tool_calls >= _budget_warn_at:
                        contents.append(
                            {
                                "role": "user",
                                "parts": [
                                    {
                                        "text": f"IMPORTANT: You have used {tool_calls} of {int(self.max_tool_calls)} available tool calls. You MUST produce your final JSON decision NOW. Do not call any more tools. Respond with your decision JSON immediately."
                                    }
                                ],
                            }
                        )
                        budget_warning_injected = True
                        logger.warning(
                            f"GEMINI_TOOL_BUDGET | finalization instruction injected at tool_calls={tool_calls}/{int(self.max_tool_calls)}"
                        )
                except Exception as e:
                    tool_trace.append({"name": "unknown", "input": {}, "result": {"success": False, "reason": str(e)}, "latency_ms": 0})
                    continue

            continue


    def _build_user_message(self, data_package: Dict, trigger_type: str = "SIGNAL") -> str:
        # Backward-compatible no-op. Main flow now uses minimal trigger_context.
        formatted_data = json.dumps(data_package or {}, indent=2, default=str)
        return f"```json\n{formatted_data}\n```"

    def _extract_first_json_object(self, content: str) -> Optional[str]:
        if not isinstance(content, str):
            return None

        if "```json" in content:
            try:
                return content.split("```json", 1)[1].split("```", 1)[0].strip() or None
            except Exception:
                pass

        if "```" in content:
            try:
                return content.split("```", 1)[1].split("```", 1)[0].strip() or None
            except Exception:
                pass

        start = content.find("{")
        if start == -1:
            return None

        in_string = False
        escape = False
        depth = 0
        obj_start = None

        for i in range(start, len(content)):
            ch = content[i]

            if in_string:
                if escape:
                    escape = False
                    continue
                if ch == "\\":
                    escape = True
                    continue
                if ch == '"':
                    in_string = False
                continue

            if ch == '"':
                in_string = True
                continue

            if ch == "{":
                if depth == 0:
                    obj_start = i
                depth += 1
                continue

            if ch == "}":
                if depth > 0:
                    depth -= 1
                    if depth == 0 and obj_start is not None:
                        candidate = content[obj_start : i + 1].strip()
                        return candidate or None

        return None

    def _parse_response(self, response: Dict, latency_ms: int) -> AgentResult:
        """
        Parse Claude's response into an AgentResult.
        
        Args:
            response: Raw API response
            latency_ms: Request latency in milliseconds
            
        Returns:
            Parsed AgentResult
        """
        content = response.get("content", "")
        
        try:
            # Extract JSON from response (may include narrative text and/or markdown code blocks)
            json_str = self._extract_first_json_object(content)

            if not json_str:
                raise json.JSONDecodeError("No JSON object found in response", content, 0)

            parsed = json.loads(json_str)
            
            # Validate required fields
            decision = parsed.get("decision", "WAIT")
            if decision not in [d.value for d in AgentDecision]:
                logger.warning(f"Invalid decision '{decision}', defaulting to WAIT")
                decision = "WAIT"
            
            # Parse v1.3 REJECT fields if present
            market_view = parsed.get("market_view")
            conditions_to_approve = parsed.get("conditions_to_approve")
            invalidation = parsed.get("invalidation")
 
            # Parse v1.4 trade plan fields if present
            trade_plan = parsed.get("trade_plan")
            if trade_plan is not None and not isinstance(trade_plan, dict):
                logger.warning("Invalid trade_plan type (expected dict) — ignoring")
                trade_plan = None
            if isinstance(trade_plan, dict):
                mm = trade_plan.get("management_mode")
                if mm is None:
                    pass
                else:
                    mm_s = str(mm).strip()
                    if mm_s not in ("ea_managed", "agent_monitored"):
                        logger.warning("Invalid trade_plan.management_mode — defaulting to ea_managed")
                        mm_s = "ea_managed"
                    trade_plan["management_mode"] = mm_s

            entry_conditions = parsed.get("entry_conditions")
            if entry_conditions is not None and not isinstance(entry_conditions, dict):
                logger.warning("Invalid entry_conditions type (expected dict) — ignoring")
                entry_conditions = None

            session_notes = None
            try:
                sn = parsed.get("session_notes")
                if sn is not None:
                    sn_s = str(sn).strip()
                    if sn_s:
                        session_notes = sn_s
            except Exception:
                session_notes = None
            
            # Parse adjustment and close_reason
            adjustment = parsed.get("adjustment")
            close_reason = parsed.get("close_reason")
            
            return AgentResult(
                decision=decision,
                confidence=int(parsed.get("confidence", 50)),
                reasoning=parsed.get("reasoning", ""),
                key_factors=parsed.get("key_factors", []),
                concerns=parsed.get("concerns", []),
                trade_plan=trade_plan,
                entry_conditions=entry_conditions,
                session_notes=session_notes,
                checklist_validation=None,
                raw_response=content,
                prompt_version=get_prompt_version(),
                prompt_hash=get_prompt_hash(),
                model=response.get("model", self.model),
                input_tokens=response.get("input_tokens", 0),
                output_tokens=response.get("output_tokens", 0),
                latency_ms=latency_ms,
                market_view=market_view,
                conditions_to_approve=conditions_to_approve,
                invalidation=invalidation,
                adjustment=adjustment,
                close_reason=close_reason,
            )
            
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse Agent response as JSON: {e}")
            logger.debug(f"Raw response: {content[:500]}")
            return self._fallback_result(f"JSON parse error: {e}", raw_response=content)

    async def _request_json_retry(self, trigger_context: str) -> Dict[str, Any]:
        loop = asyncio.get_event_loop()
        retry_prompt = (
            "Your previous response was not valid JSON. "
            "Respond with ONLY your decision as valid JSON now."
        )
        system_prompt = ""
        try:
            system_prompt = get_system_prompt()
        except Exception:
            system_prompt = ""

        contents: List[Dict[str, Any]] = []
        if system_prompt:
            contents.append({"role": "user", "parts": [{"text": system_prompt}]})
        contents.append({"role": "user", "parts": [{"text": str(trigger_context or "").strip()}]})
        contents.append({"role": "user", "parts": [{"text": retry_prompt}]})

        def _sync_retry_call():
            return self.client.models.generate_content(
                model=self.model,
                contents=contents,
                config={
                    "max_output_tokens": int(self.max_tokens),
                    "temperature": 0.1,
                },
            )

        resp = await loop.run_in_executor(None, _sync_retry_call)

        input_tokens = 0
        output_tokens = 0
        try:
            usage = getattr(resp, "usage_metadata", None)
            if usage is not None:
                input_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
                output_tokens = int(getattr(usage, "candidates_token_count", 0) or 0)
        except Exception:
            input_tokens = 0
            output_tokens = 0

        content = ""
        try:
            content = getattr(resp, "text", None) or ""
        except Exception:
            content = ""

        return {
            "content": content,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "model": self.model,
            "tool_trace": [],
        }

    async def _parse_response_with_retry(self, response: Dict, latency_ms: int, trigger_context: str) -> AgentResult:
        content = response.get("content", "")

        if self._looks_like_non_json_text(content):
            logger.warning("AGENT_JSON_RETRY | non-JSON response detected, retrying once")
            try:
                retry_response = await self._request_json_retry(trigger_context)
                response = {
                    "content": retry_response.get("content", ""),
                    "input_tokens": int(response.get("input_tokens", 0) or 0) + int(retry_response.get("input_tokens", 0) or 0),
                    "output_tokens": int(response.get("output_tokens", 0) or 0) + int(retry_response.get("output_tokens", 0) or 0),
                    "model": retry_response.get("model", response.get("model", self.model)),
                    "tool_trace": response.get("tool_trace", []),
                }
                logger.warning(f"AGENT_JSON_RETRY | retry_response_preview={str(response.get('content', ''))[:200]}")
            except Exception as e:
                logger.warning(f"AGENT_JSON_RETRY | retry failed: {e}")

        return self._parse_response(response, latency_ms)

    def _fallback_result(self, error: str, raw_response: str = None) -> AgentResult:
        """
        Create a fallback result when Agent fails.
        
        Args:
            error: Error message
            raw_response: Optional raw response for debugging
            
        Returns:
            AgentResult with DEFER_TO_BRAIN decision
        """
        return AgentResult(
            decision=AgentDecision.DEFER_TO_BRAIN.value,
            confidence=0,
            reasoning=f"Agent fallback: {error}",
            key_factors=[],
            concerns=[],
            raw_response=raw_response,
            prompt_version=get_prompt_version(),
            prompt_hash=get_prompt_hash(),
            model=self.model or "unknown",
            error=error,
        )

    def is_enabled(self) -> bool:
        """Check if Agent is enabled and initialized"""
        return self.enabled and self._initialized

    def get_mode(self) -> str:
        """Get current Agent mode (shadow/gate/full)"""
        return self.mode


# Global singleton instance
_agent_instance: Optional[AIAgent] = None


def get_agent() -> AIAgent:
    """Get or create the global Agent instance"""
    global _agent_instance
    if _agent_instance is None:
        _agent_instance = AIAgent()
    return _agent_instance


def initialize_agent() -> bool:
    """Initialize the global Agent instance"""
    agent = get_agent()
    return agent.initialize()


async def agent_decide(
    trigger_context: Any,
    tools: Any,
    trigger_type: str = "SIGNAL",
    allow_memory_write: bool = True,
) -> AgentResult:
    """
    Convenience function to get Agent decision.
    Handles memory injection and saving.
    
    Args:
        trigger_context: Minimal context describing why the Agent was called
        tools: AgentTools instance
        
    Returns:
        AgentResult with decision
    """
    agent = get_agent()
    
    # Inject memory context into trigger_context if trigger_context is a dict (backward compat)
    if trigger_type != "PROACTIVE_H1" and isinstance(trigger_context, dict):
        try:
            from agent_memory import get_memory_context_for_agent
            memory_context = get_memory_context_for_agent()
            if memory_context:
                trigger_context["agent_memory_context"] = memory_context
                logger.debug(f"Injected memory context: all_conditions_met={memory_context.get('all_conditions_met')}")
        except Exception as e:
            logger.warning(f"Failed to inject memory context: {e}")

    # Get Agent decision
    result = await agent.decide(trigger_context, tools=tools, trigger_type=trigger_type)

    try:
        parsed_obj = None
        raw_response = result.raw_response or ""
        try:
            json_str = agent._extract_first_json_object(raw_response)
            if json_str:
                parsed_obj = json.loads(json_str)
        except Exception:
            parsed_obj = None

        if isinstance(parsed_obj, dict):
            # disabled — incompatible with tool-use architecture
            # result.checklist_validation = _validate_checklist(parsed_obj, trigger_context if isinstance(trigger_context, dict) else {})
            pass
        else:
            logger.warning("AGENT_CHECKLIST | MISSING — could not re-parse raw_response JSON for checklist validation")
            logger.warning(f"AGENT_CHECKLIST | MISSING | raw_response_preview={str(raw_response)[:500]}")
    except Exception as e:
        logger.debug(f"AGENT_CHECKLIST | validation failed (non-blocking): {e}")

    try:
        if result.session_notes:
            session_context = None
            session_context = None
            if isinstance(trigger_context, dict):
                try:
                    session_context = (trigger_context.get("session") or {}).get("context")
                except Exception:
                    session_context = None
                if not session_context:
                    session_context = trigger_context.get("session")
            _update_session_memory(result.session_notes, session_context=session_context)
    except Exception as e:
        logger.debug(f"Session memory persist failed (non-blocking): {e}")
    
    # Save REJECT to memory (v1.3)
    if allow_memory_write and result.decision == "REJECT":
        if result.market_view and result.conditions_to_approve:
            try:
                from agent_memory import save_reject
                brain = trigger_context.get("brain_analysis", {}) if isinstance(trigger_context, dict) else {}
                save_reject(
                    brain_signal=brain.get("decision", "UNKNOWN"),
                    brain_score=brain.get("score", 50),
                    market_view_direction=result.market_view.get("direction", "HOLD"),
                    market_view_description=result.market_view.get("description", ""),
                    conditions=result.conditions_to_approve,
                    invalidation_str=result.invalidation or "3 H1 candles",
                )
                logger.info(f"Saved REJECT to memory: view={result.market_view.get('direction')}, {len(result.conditions_to_approve)} conditions")
            except Exception as e:
                logger.warning(f"Failed to save REJECT to memory: {e}")
        else:
            logger.warning(f"REJECT not saved to memory — missing required v1.3 fields (market_view={result.market_view is not None}, conditions_to_approve={result.conditions_to_approve is not None})")
    
    return result


# =============================================================================
# TESTS
# =============================================================================

async def _test_agent():
    """Test the AI Agent with mock data"""
    print("=" * 60)
    print("🤖 AI AGENT TEST")
    print("=" * 60)
    
    # Mock data package
    mock_data = {
        "timestamp": datetime.utcnow().isoformat(),
        "current_price": {"bid": 2915.50, "ask": 2915.80, "spread": 3.0},
        "brain_analysis": {
            "decision": "BUY",
            "score": 68.2,
            "confidence": 72,
            "scenario": "momentum_forte_confirmado",
        },
        "indicators": {
            "rsi": {"value": 62, "level": "neutral"},
            "adx": {"value": 32, "plus_di": 28, "minus_di": 18},
        },
        "session": {"name": "London"},
    }
    
    agent = get_agent()
    
    # Test without initialization
    print("\n1. Test without initialization:")
    result = await agent.decide("Test trigger", tools=None)
    print(f"   Decision: {result.decision}")
    print(f"   Error: {result.error}")
    
    # Test initialization (will fail without API key)
    print("\n2. Test initialization:")
    success = agent.initialize()
    print(f"   Initialized: {success}")
    print(f"   Enabled: {agent.is_enabled()}")
    
    if agent.is_enabled():
        print("\n3. Test with real API call:")
        result = await agent.decide("Test trigger", tools=None)
        print(f"   Decision: {result.decision}")
        print(f"   Confidence: {result.confidence}")
        print(f"   Reasoning: {result.reasoning[:100]}...")
        print(f"   Tokens: {result.input_tokens} in, {result.output_tokens} out")
        print(f"   Latency: {result.latency_ms}ms")


if __name__ == "__main__":
    asyncio.run(_test_agent())
