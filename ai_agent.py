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
import config

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

        # FLO-241: Dedup — same logic as agent_tools.write_session_memory
        _skip_append = False
        try:
            import re as _re_sm
            _SYN = {"middle": "center", "box": "range", "reclaim": "push",
                    "under": "below", "wake": "reassess", "business": "trade",
                    "unchanged": "same", "framework": "thesis", "lean": "consider",
                    "actionable": "tradeable", "acceptance": "confirmation",
                    "continuation": "extension", "opens": "targets",
                    "stay": "remain", "flat": "idle", "decisive": "clear",
                    "engage": "enter", "respect": "watch", "especially": "particularly"}
            _STOP = {"a", "an", "the", "is", "in", "on", "of", "to", "for",
                     "and", "or", "but", "not", "this", "that", "with", "from",
                     "at", "by", "do", "if", "it", "my", "no", "so", "be", "i"}
            def _sm_norm(s):
                s = s.lower().strip()
                s = _re_sm.sub(r'\d{4,}\.?\d*', 'PRICE', s)
                s = _re_sm.sub(r'[.,;:!?()"\'\-/]', ' ', s)
                s = _re_sm.sub(r'\s+', ' ', s)
                words = [_SYN.get(w, w) for w in s.split() if w not in _STOP and len(w) > 1]
                return ' '.join(words)
            _new_norm = _sm_norm(notes_s)[:120]
            _new_words = set(_new_norm.split())
            for _existing_n in (payload.get("notes") or []):
                _ex_text = _existing_n.get("note", _existing_n.get("text", "")) if isinstance(_existing_n, dict) else str(_existing_n)
                if isinstance(_existing_n, dict) and str(_existing_n.get("source") or "").lower() == "sage":
                    continue
                _ex_norm = _sm_norm(_ex_text)[:120]
                _ex_words = set(_ex_norm.split())
                if _new_words and _ex_words:
                    _overlap = len(_new_words & _ex_words) / max(len(_new_words), len(_ex_words))
                    if _overlap >= 0.55:
                        _skip_append = True
                        logger.debug(f"SESSION_MEMORY | DEDUP_SKIP | overlap={_overlap:.2f}")
                        break
        except Exception:
            pass

        if not _skip_append:
            payload["notes"].append({"time": now.strftime("%H:%M"), "note": notes_s})

        # Keep max 8 notes, protect Sage notes from truncation.
        try:
            all_notes = payload.get("notes") or []
            sage_notes = []
            normal_notes = []
            for n in all_notes:
                if isinstance(n, dict) and str(n.get("source") or "").strip().lower() == "sage":
                    sage_notes.append(n)
                else:
                    normal_notes.append(n)
            normal_notes = normal_notes[-7:]
            payload["notes"] = normal_notes + sage_notes
            payload["notes"] = payload["notes"][-8:]
        except Exception:
            payload["notes"] = payload["notes"][-8:]
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
    data_needs: Optional[str] = None  # Diagnostic: what data Floki wanted but couldn't access
    rex_agreed: Optional[bool] = None  # FLO-158: kept for DB compat, always None now
    rex_reasoning: Optional[str] = None
    rex_insights: Optional[list] = None  # FLO-158: new insights format

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
        if self.data_needs:
            result["data_needs"] = self.data_needs
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
        if self.rex_agreed is not None:
            result["rex_agreed"] = self.rex_agreed
        if self.rex_reasoning:
            result["rex_reasoning"] = self.rex_reasoning
        if self.rex_insights:
            result["rex_insights"] = self.rex_insights
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
        Initialize the OpenAI client (FLO-130: migrated from Gemini).
        Call this after config is loaded.
        """
        try:
            import config

            self.enabled = getattr(config, 'USE_AI_AGENT', False)
            if not self.enabled:
                logger.info("AI Agent is disabled in config")
                return False

            # FLO-247: Qwen primary, GPT-5.4 fallback
            _qwen_key = getattr(config, 'FLOKI_API_KEY', '') or os.environ.get("QWEN_API_KEY", "")
            _qwen_base = getattr(config, 'FLOKI_API_BASE', '')
            _openai_key = os.environ.get("OPENAI_API_KEY", "")

            if not _qwen_key and not _openai_key:
                logger.warning("No API key set (QWEN_API_KEY / OPENAI_API_KEY) - AI Agent disabled")
                self.enabled = False
                return False

            try:
                from openai import OpenAI
                if _qwen_key and _qwen_base:
                    self.client = OpenAI(api_key=_qwen_key, base_url=_qwen_base, timeout=90, max_retries=0)
                    logger.info(f"AI Agent: primary client = Qwen ({_qwen_base}, timeout=90s)")
                else:
                    self.client = OpenAI(api_key=_openai_key)
                    logger.info("AI Agent: primary client = OpenAI (no Qwen key)")

                # Fallback client (GPT-5.4)
                self._fallback_client = None
                self._fallback_model = getattr(config, 'FLOKI_FALLBACK_MODEL', 'gpt-5.4')
                if _openai_key:
                    self._fallback_client = OpenAI(api_key=_openai_key)
            except ImportError:
                logger.error("openai package not installed. Run: pip install openai")
                self.enabled = False
                return False
            except Exception as e:
                logger.error(f"Failed to initialize OpenAI client: {e}")
                self.enabled = False
                return False

            self.model = getattr(config, 'FLOKI_MODEL', 'qwen3.6-plus')
            self.timeout = getattr(config, 'AI_AGENT_TIMEOUT', 240)
            self.mode = getattr(config, 'AI_AGENT_MODE', 'shadow')
            self.max_tool_calls = int(getattr(config, 'AI_AGENT_MAX_TOOL_CALLS', 40) or 40)
            self.max_tokens = int(getattr(config, 'AI_AGENT_MAX_TOKENS', 4096) or 4096)

            self._initialized = True
            logger.info(f"AI Agent initialized: model={self.model}, mode={self.mode}, timeout={self.timeout}s")
            return True

        except Exception as e:
            logger.error(f"Failed to initialize AI Agent: {e}")
            self.enabled = False
            return False

    async def decide(self, trigger_context: Any, tools: Any, trigger_type: str = "SIGNAL", chart_images: Optional[dict] = None) -> AgentResult:
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
                self._call_openai_with_tools(user_message, tools=tools, chart_images=chart_images),
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
            
            # FLO-158: attach Rex insights to result for DB storage
            try:
                rex_hist = getattr(tools, "_rex_debate_history", [])
                if rex_hist:
                    last_rex = rex_hist[-1]
                    result.rex_insights = last_rex.get("insights", [])
                    result.rex_reasoning = (last_rex.get("rex") or "")[:4000]
                    # rex_agreed stays None (FLO-158: no longer used)
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
        """Return get_headlines — always available. get_macro removed (FLO-156: 80% dead after FLO-121)."""
        return [
            {
                "name": "get_headlines",
                "description": "Get cached news headlines (max 10)",
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
            {
                "name": "get_pivot_points",
                "description": "Get daily Classic and Fibonacci Pivot Points (R3/R2/R1/PP/S1/S2/S3) from previous D1 candle",
                "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            # get_headlines: always available (get_macro removed FLO-156)
            *self._macro_tools_if_needed(),
            {
                "name": "get_calendar",
                "description": "Get cached economic calendar phase + next event",
                "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            # FLO-187: Only register ML tool when ML is enabled
            *([] if not config.ML_ENABLED else [{
                "name": "get_ml_prediction",
                "description": "Get cached ML prediction snapshot",
                "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            }]),
            {
                "name": "get_market_context",
                "description": "Get markets correlated with gold — metals (silver, platinum, palladium + gold/silver ratio), forex (dollar strength, safe havens), indices (S&P 500), energy (oil), crypto (BTC), and futures (DXY, VIX, 10Y Bond). Each instrument includes bid, change %, and position in today's range.",
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
                "description": "After opening a trade, set watch conditions for Simba to monitor the position. Supported types: price_touch (level + optional tolerance fields — triggers when price reaches level), pnl_threshold (value in dollars — negative for loss alert e.g. -15, positive for profit alert e.g. 20), pnl_below (value in dollars — triggers when profit drops BELOW value, e.g. {type: 'pnl_below', value: 10} wakes you when profit drops below $10), indicator_threshold (indicator + direction + level fields — supports: rsi, macd_histogram, adx, vix. E.g. {type: 'indicator_threshold', indicator: 'rsi', direction: 'below', level: 40, description: 'RSI momentum collapse'}).",
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
                "description": "Adjust SL/TP of an open trade. You are responsible for position management — move SL to protect profits or adjust TP based on market structure.",
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
                "name": "get_recent_reflexions",
                "description": "Read your most recent post-trade reflexions — what you learned from each closed trade (thesis correctness, key lesson, pattern tags). Call to review recent performance.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "search_reflexions",
                "description": "Search past trade reflexions by keywords (e.g., 'false_breakout', 'asian_session', 'sl_too_tight'). Returns trades matching any keyword, sorted by relevance.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "keywords": {"type": "string", "description": "Space-separated keywords to search in lessons and pattern tags"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
                    },
                    "required": ["keywords"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "search_memory",
                "description": "Semantic search across your trade reflexions using natural language. Finds similar past situations even with different wording. Example: 'trade that failed near a round number during low volume' or 'successful breakout after consolidation'. Falls back to search_reflexions if unavailable.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Natural language description of the trade pattern or situation you want to find"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 3},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
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
            },
            {
                "name": "set_next_check",
                "description": "Schedule your next analysis cycle. Range: 2-120 minutes.",
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
            {
                "name": "get_rex_monitor",
                "description": "Get Rex's proactive monitor scan — divergences, correlations, regime changes, session performance. Rex scans every 30 min independently. Returns alert_level (QUIET/NORMAL/ELEVATED/CRITICAL) and findings.",
                "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            # FLO-243: get_oracle_verdict removed — verdict now auto-injected at end of trigger_context
            {
                "name": "write_trading_journal",
                "description": "Write a persistent journal entry (reflection, lesson, frustration, idea, missing_data, market_observation). Accumulates over days — your product owner reads this.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "entry": {"type": "string"},
                        "category": {"type": "string"},
                    },
                    "required": ["entry"],
                    "additionalProperties": False,
                },
            },
        ]

    def _openai_tools(self) -> List[Dict[str, Any]]:
        """Convert tool schemas to OpenAI function-calling format."""
        tools: List[Dict[str, Any]] = []
        for t in self._tool_schemas():
            try:
                name = t.get("name")
                desc = t.get("description")
                schema = t.get("input_schema")
                if not name or not isinstance(schema, dict):
                    continue
                tools.append({
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": desc or "",
                        "parameters": copy.deepcopy(schema),
                    },
                })
            except Exception:
                continue
        return tools

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

    async def _call_openai_with_tools(self, trigger_context: str, tools: Any, chart_images: Optional[dict] = None) -> Dict[str, Any]:
        """FLO-130: OpenAI GPT-5.4 tool loop (migrated from Gemini)."""
        loop = asyncio.get_event_loop()
        start_time = time.time()
        # Clean up per-iteration retry flags from previous cycles
        for attr in [k for k in self.__dict__ if k.startswith("_retry_iter_")]:
            delattr(self, attr)

        total_input_tokens = 0
        total_output_tokens = 0
        _last_prompt_tokens = 0
        last_model = self.model
        tool_calls_count = 0
        tool_trace: List[Dict[str, Any]] = []
        had_execution_followup = False

        system_prompt = ""
        try:
            system_prompt = get_system_prompt()
        except Exception:
            system_prompt = ""

        openai_tools = self._openai_tools()

        messages: List[Dict[str, Any]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        # Build user message: text + optional chart images (GPT-5.4 vision)
        _tc_text = str(trigger_context or "").strip()
        if chart_images and chart_images.get("success"):
            content_blocks: list = [{"type": "text", "text": _tc_text}]
            if chart_images.get("h1_b64"):
                content_blocks.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{chart_images['h1_b64']}",
                        "detail": "high",
                    },
                })
                content_blocks.append({"type": "text", "text": "Above: XAUUSD H1 chart. Analyze candle patterns, S/R interactions, and momentum visually."})
            if chart_images.get("m15_b64"):
                content_blocks.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{chart_images['m15_b64']}",
                        "detail": "high",
                    },
                })
                content_blocks.append({"type": "text", "text": "Above: XAUUSD M15 chart. Analyze short-term structure and entry timing."})
            messages.append({"role": "user", "content": content_blocks})
            logger.info(f"FLOKI | vision: h1={'yes' if chart_images.get('h1_b64') else 'no'} m15={'yes' if chart_images.get('m15_b64') else 'no'}")
        else:
            messages.append({"role": "user", "content": _tc_text})

        PER_CALL_TIMEOUT = 90  # httpx timeout set at client level; this is a fallback
        MAX_ITERATIONS = int(self.max_tool_calls) + 2

        for iteration in range(MAX_ITERATIONS):
            if (time.time() - start_time) >= float(self.timeout):
                logger.warning(f"FLOKI | tool loop timeout after {iteration} iterations")
                return {
                    "content": json.dumps({"decision": "DEFER_TO_BRAIN", "confidence": 0, "reasoning": "timeout during tool loop", "key_factors": [], "concerns": ["timeout"]}),
                    "input_tokens": total_input_tokens, "output_tokens": total_output_tokens, "context_tokens": _last_prompt_tokens, "model": last_model, "tool_trace": tool_trace,
                }

            try:
                _use_client = self.client
                _use_model = self.model
                # FLO-247: fallback to GPT-5.4 if primary failed on previous iteration
                if getattr(self, '_using_fallback', False) and self._fallback_client:
                    _use_client = self._fallback_client
                    _use_model = self._fallback_model

                def _sync_call():
                    kwargs = {
                        "model": _use_model,
                        "messages": messages,
                        "tools": openai_tools,
                        "max_completion_tokens": int(self.max_tokens),
                        "temperature": 1.0,
                        "timeout": PER_CALL_TIMEOUT,
                    }
                    # Only force JSON output when no tool calls are expected (avoids degrading tool use)
                    if not openai_tools:
                        kwargs["response_format"] = {"type": "json_object"}
                    return _use_client.chat.completions.create(**kwargs)
                resp = await loop.run_in_executor(None, _sync_call)
                self._using_fallback = False  # reset on success
            except Exception as e:
                logger.warning(f"FLOKI | API call failed (iteration {iteration}): {e}")
                # Retry once on primary (Qwen) before falling back to GPT
                _retry_key = f"_retry_iter_{iteration}"
                if not getattr(self, '_using_fallback', False) and not getattr(self, _retry_key, False):
                    setattr(self, _retry_key, True)
                    logger.info(f"FLOKI | retrying iteration {iteration} on primary model")
                    await asyncio.sleep(2)
                    continue
                # FLO-247: switch to fallback after retry exhausted
                if not getattr(self, '_using_fallback', False) and self._fallback_client:
                    logger.warning(f"FLOKI | switching to fallback model: {self._fallback_model}")
                    self._using_fallback = True
                await asyncio.sleep(2)
                continue

            try:
                usage = resp.usage
                if usage:
                    _last_prompt_tokens = usage.prompt_tokens or 0
                    total_input_tokens += _last_prompt_tokens
                    total_output_tokens += usage.completion_tokens or 0
                    last_model = resp.model or self.model
            except Exception:
                pass

            # Guard for empty choices (content filter, safety refusal)
            if not resp.choices:
                logger.warning(f"FLOKI | empty choices at iteration {iteration} — content filter or safety refusal")
                continue

            msg = resp.choices[0].message
            finish = resp.choices[0].finish_reason

            has_tool_calls = bool(msg.tool_calls)
            has_content = bool(msg.content and msg.content.strip())
            logger.info(
                f"FLOKI_TURN | finish={finish} tool_calls={len(msg.tool_calls) if msg.tool_calls else 0} "
                f"has_content={has_content} iteration={iteration} tools_used={tool_calls_count}/{int(self.max_tool_calls)}"
            )

            if msg.tool_calls:
                # Check if we have budget for ALL tool calls in this batch
                remaining_budget = int(self.max_tool_calls) - tool_calls_count
                calls_to_process = msg.tool_calls[:remaining_budget] if remaining_budget < len(msg.tool_calls) else msg.tool_calls

                # Build assistant message with ONLY the calls we'll process (avoids orphan tool_call_ids)
                if len(calls_to_process) < len(msg.tool_calls):
                    logger.warning(f"FLOKI | tool budget hit: processing {len(calls_to_process)}/{len(msg.tool_calls)} calls")
                    # Create a modified message with only the calls we process
                    messages.append({"role": "assistant", "tool_calls": [
                        {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                        for tc in calls_to_process
                    ]})
                else:
                    messages.append(msg)

                for tc in calls_to_process:
                    tool_calls_count += 1
                    fname = tc.function.name
                    try:
                        fargs = json.loads(tc.function.arguments) if tc.function.arguments else {}
                    except Exception:
                        fargs = {}
                    t0 = time.time()
                    result = self._execute_tool(tools, fname, fargs)
                    dt_ms = int((time.time() - t0) * 1000)
                    tool_trace.append({"name": fname, "input": fargs, "result": result, "latency_ms": dt_ms})
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result, ensure_ascii=False, default=str)})
                continue

            text_out = msg.content or ""

            if text_out.strip():
                if not had_execution_followup:
                    _trace_names = {str(t.get("name", "")).lower() for t in tool_trace if isinstance(t, dict)}

                    # Parse the decision field from JSON to avoid false positives
                    # ("decided against OPEN_BUY" should NOT trigger followup)
                    _parsed_decision = ""
                    _parsed_json = None
                    try:
                        _parsed_json = json.loads(text_out)
                        _parsed_decision = str(_parsed_json.get("decision", "")).upper()
                    except (json.JSONDecodeError, TypeError, AttributeError):
                        _parsed_decision = ""

                    _needs_execute = _parsed_decision in ("OPEN_BUY", "OPEN_SELL") and "execute_trade" not in _trace_names
                    _needs_close = _parsed_decision == "CLOSE_TRADE" and "close_trade" not in _trace_names
                    _needs_adjust = _parsed_decision == "ADJUST_TRADE" and "adjust_trade" not in _trace_names

                    if _needs_execute or _needs_close or _needs_adjust:
                        _missing_tool = "execute_trade" if _needs_execute else ("close_trade" if _needs_close else "adjust_trade")
                        _followup_msg = (
                            f"Your decision above requires calling {_missing_tool} to take effect. "
                            f"Call {_missing_tool} now with the correct parameters, "
                            f"or respond with your updated decision if you changed your mind."
                        )
                        had_execution_followup = True
                        messages.append({"role": "user", "content": _followup_msg})
                        logger.info(f"FLOKI_FOLLOWUP | missing={_missing_tool} | injecting follow-up turn (tools_used={tool_calls_count}/{int(self.max_tool_calls)})")
                        continue

                    # FLO-230: Decision override — actions trump words.
                    # If a trading tool was already executed, the decision MUST reflect it.
                    if _parsed_json and "execute_trade" in _trace_names and _parsed_decision not in ("OPEN_BUY", "OPEN_SELL"):
                        _exec_dir = None
                        for _tt in tool_trace:
                            if _tt.get("name") == "execute_trade":
                                _exec_dir = str(_tt.get("input", {}).get("direction", "")).upper()
                                break
                        if _exec_dir in ("BUY", "SELL"):
                            _override = f"OPEN_{_exec_dir}"
                            logger.warning(f"DECISION_OVERRIDE | said {_parsed_decision} but execute_trade({_exec_dir}) was called | overriding to {_override}")
                            _parsed_json["decision"] = _override
                            text_out = json.dumps(_parsed_json, ensure_ascii=False)

                    if _parsed_json and "close_trade" in _trace_names and _parsed_decision != "CLOSE_TRADE":
                        logger.warning(f"DECISION_OVERRIDE | said {_parsed_decision} but close_trade was called | overriding to CLOSE_TRADE")
                        _parsed_json["decision"] = "CLOSE_TRADE"
                        text_out = json.dumps(_parsed_json, ensure_ascii=False)

                    if _parsed_json and "adjust_trade" in _trace_names and _parsed_decision != "ADJUST_TRADE":
                        logger.warning(f"DECISION_OVERRIDE | said {_parsed_decision} but adjust_trade was called | overriding to ADJUST_TRADE")
                        _parsed_json["decision"] = "ADJUST_TRADE"
                        text_out = json.dumps(_parsed_json, ensure_ascii=False)

                # Cost per model ($/M tokens)
                _is_qwen = "qwen" in (last_model or "").lower()
                _cost_in = total_input_tokens * (0.50 if _is_qwen else 2.50) / 1_000_000
                _cost_out = total_output_tokens * (2.00 if _is_qwen else 15.00) / 1_000_000
                _cost_total = _cost_in + _cost_out
                _latency = int((time.time() - start_time) * 1000)
                logger.info(
                    f"FLOKI | model={last_model} | ctx={_last_prompt_tokens} | sum_in={total_input_tokens} | out={total_output_tokens} | "
                    f"cost=${_cost_total:.4f} | tools_called={tool_calls_count} | latency={_latency}ms"
                )
                return {"content": text_out.strip(), "input_tokens": total_input_tokens, "output_tokens": total_output_tokens, "context_tokens": _last_prompt_tokens, "model": last_model, "tool_trace": tool_trace}

            logger.warning(f"FLOKI | empty response at iteration {iteration}, finish={finish}")
            continue

        logger.warning(f"FLOKI | loop exhausted after {MAX_ITERATIONS} iterations")
        return {
            "content": json.dumps({"decision": "DEFER_TO_BRAIN", "confidence": 0, "reasoning": "loop exhausted", "key_factors": [], "concerns": ["loop_exhausted"]}),
            "input_tokens": total_input_tokens, "output_tokens": total_output_tokens, "context_tokens": _last_prompt_tokens, "model": last_model, "tool_trace": tool_trace,
        }


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
                logger.debug("Invalid entry_conditions type (expected dict) — ignoring")
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

            # Diagnostic: data_needs (optional, never affects decisions)
            data_needs = None
            _dn_raw = parsed.get("data_needs")
            if isinstance(_dn_raw, str) and _dn_raw.strip():
                data_needs = _dn_raw.strip()[:500]
                logger.info(f"FLOKI_DATA_NEEDS | {data_needs}")

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
                data_needs=data_needs,
            )

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse Agent response as JSON: {e}")
            logger.debug(f"Raw response: {content[:500]}")
            return self._fallback_result(f"JSON parse error: {e}", raw_response=content)

    async def _request_json_retry(self, trigger_context: str) -> Dict[str, Any]:
        """FLO-130: JSON retry via OpenAI."""
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

        messages: List[Dict[str, Any]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": str(trigger_context or "").strip()})
        messages.append({"role": "user", "content": retry_prompt})

        def _sync_retry_call():
            return self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                response_format={"type": "json_object"},
                max_completion_tokens=int(self.max_tokens),
                temperature=0.7,
                timeout=20,
            )

        resp = await loop.run_in_executor(None, _sync_retry_call)

        input_tokens = 0
        output_tokens = 0
        try:
            usage = resp.usage
            if usage:
                input_tokens = usage.prompt_tokens or 0
                output_tokens = usage.completion_tokens or 0
        except Exception:
            pass

        content = ""
        try:
            content = resp.choices[0].message.content or ""
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
            # Try extracting JSON from thinking+response text first (saves 30-60s retry)
            _extracted = self._extract_first_json_object(content)
            if _extracted:
                try:
                    json.loads(_extracted)  # validate it's real JSON
                    response["content"] = _extracted
                    content = _extracted
                    logger.info("AGENT_JSON_RETRY | extracted JSON from thinking text (no retry needed)")
                except Exception:
                    _extracted = None

            if not _extracted:
                # Extraction failed — fall back to retry API call
                logger.warning("AGENT_JSON_RETRY | non-JSON response, retrying once")
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
    chart_images: Optional[dict] = None,
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
    result = await agent.decide(trigger_context, tools=tools, trigger_type=trigger_type, chart_images=chart_images)

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
