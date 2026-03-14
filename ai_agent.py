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

        if str(payload.get("session_date") or "") != today:
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
                "notes": [],
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
        payload["notes"] = payload["notes"][-10:]
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
        Initialize the Anthropic client.
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
            api_key = os.environ.get("ANTHROPIC_API_KEY", "")
            if not api_key:
                logger.warning("ANTHROPIC_API_KEY not set - AI Agent disabled")
                self.enabled = False
                return False
            
            # Import and initialize Anthropic client
            try:
                from anthropic import Anthropic
                self.client = Anthropic(api_key=api_key)
            except ImportError:
                logger.error("anthropic package not installed. Run: pip install anthropic")
                self.enabled = False
                return False
            
            # Get config
            self.model = getattr(config, 'AI_AGENT_MODEL', 'claude-sonnet-4-20250514')
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
                self._call_claude_with_tools(user_message, tools=tools),
                timeout=self.timeout,
            )
            
            # Calculate latency
            latency_ms = int((datetime.utcnow() - start_time).total_seconds() * 1000)
            
            result = self._parse_response(response, latency_ms)

            try:
                if response.get("tool_trace"):
                    # Preserve original raw JSON text and append tool trace in a safe way.
                    # Downstream parsers use JSON extraction from raw_response; keep it intact.
                    result.raw_response = (result.raw_response or "")
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

    def _tool_schemas(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "get_current_price",
                "description": "Get current bid/ask/spread from cached Brain data",
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
                "name": "get_headlines",
                "description": "Get cached news headlines (max 10)",
                "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "get_macro",
                "description": "Get cached macro snapshot (DXY, VIX, yields, etc.)",
                "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
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
                "description": "Execute a trade. Enforces safety and risk rules in code.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "direction": {"type": "string", "enum": ["BUY", "SELL"]},
                        "sl": {"type": "number"},
                        "tp": {"type": "number"},
                    },
                    "required": ["direction", "sl", "tp"],
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
        ]

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

    async def _call_claude_with_tools(self, trigger_context: str, tools: Any) -> Dict[str, Any]:
        loop = asyncio.get_event_loop()
        start_time = time.time()

        tool_schemas = self._tool_schemas()
        messages: List[Dict[str, Any]] = [
            {"role": "user", "content": trigger_context}
        ]

        total_input_tokens = 0
        total_output_tokens = 0
        last_model = self.model

        tool_calls = 0
        tool_trace: List[Dict[str, Any]] = []

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
                return self.client.messages.create(
                    model=self.model,
                    max_tokens=int(self.max_tokens),
                    system=get_system_prompt(),
                    tools=tool_schemas,
                    messages=messages,
                )

            response = await loop.run_in_executor(None, _sync_call)

            try:
                total_input_tokens += int(getattr(response.usage, "input_tokens", 0) or 0)
                total_output_tokens += int(getattr(response.usage, "output_tokens", 0) or 0)
            except Exception:
                pass

            try:
                last_model = getattr(response, "model", last_model)
            except Exception:
                pass

            content_blocks = list(getattr(response, "content", []) or [])
            if not content_blocks:
                # Treat as terminal failure
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

            tool_uses = []
            text_parts = []
            for block in content_blocks:
                btype = getattr(block, "type", None)
                if btype == "tool_use":
                    tool_uses.append(block)
                elif btype == "text":
                    t = getattr(block, "text", "")
                    if t:
                        text_parts.append(t)

            if text_parts and not tool_uses:
                return {
                    "content": "\n".join(text_parts).strip(),
                    "input_tokens": total_input_tokens,
                    "output_tokens": total_output_tokens,
                    "model": last_model,
                    "tool_trace": tool_trace,
                }

            # Otherwise, execute tool calls and continue loop.
            if tool_uses:
                messages.append(
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": getattr(tu, "id", ""),
                                "name": getattr(tu, "name", ""),
                                "input": getattr(tu, "input", {}) or {},
                            }
                            for tu in tool_uses
                        ],
                    }
                )

                tool_results_blocks = []
                for tu in tool_uses:
                    if tool_calls >= int(self.max_tool_calls):
                        break
                    tool_calls += 1
                    name = str(getattr(tu, "name", "") or "").strip()
                    tid = str(getattr(tu, "id", "") or "").strip()
                    inp = getattr(tu, "input", {}) or {}

                    t0 = time.time()
                    result = self._execute_tool(tools, name, inp if isinstance(inp, dict) else {})
                    dt_ms = int((time.time() - t0) * 1000)

                    tool_trace.append(
                        {
                            "name": name,
                            "input": inp,
                            "result": result,
                            "latency_ms": dt_ms,
                        }
                    )

                    tool_results_blocks.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tid,
                            "content": json.dumps(result, ensure_ascii=False, default=str),
                        }
                    )

                messages.append({"role": "user", "content": tool_results_blocks})
                continue

            # If we got here, we had text + tool_uses. Ignore the text and continue tool loop;
            # the final answer should be text-only.
            continue


    def _build_user_message(self, data_package: Dict, trigger_type: str = "SIGNAL") -> str:
        # Backward-compatible no-op. Main flow now uses minimal trigger_context.
        formatted_data = json.dumps(data_package or {}, indent=2, default=str)
        return f"```json\n{formatted_data}\n```"

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
            json_str = None

            if "```json" in content:
                json_str = content.split("```json", 1)[1].split("```", 1)[0].strip()
            elif "```" in content:
                json_str = content.split("```", 1)[1].split("```", 1)[0].strip()
            else:
                # Fallback: find first '{' and last '}' and parse that substring.
                # This handles mixed narrative + naked JSON.
                start = content.find("{")
                end = content.rfind("}")
                if start != -1 and end != -1 and end > start:
                    json_str = content[start : end + 1].strip()

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
                # Optional trade management fields (Commit 2)
                for k in ("breakeven_trigger", "trailing_trigger", "trailing_distance"):
                    v = trade_plan.get(k)
                    if v is None:
                        continue
                    try:
                        trade_plan[k] = float(v)
                    except Exception:
                        logger.warning(f"Invalid trade_plan.{k} (expected number or null) — ignoring")
                        trade_plan[k] = None

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
        try:
            content = result.raw_response or ""
            json_str = None
            if "```json" in content:
                json_str = content.split("```json", 1)[1].split("```", 1)[0].strip()
            elif "```" in content:
                json_str = content.split("```", 1)[1].split("```", 1)[0].strip()
            else:
                start = content.find("{")
                end = content.rfind("}")
                if start != -1 and end != -1 and end > start:
                    json_str = content[start : end + 1].strip()
            if json_str:
                parsed_obj = json.loads(json_str)
        except Exception:
            parsed_obj = None

        if isinstance(parsed_obj, dict):
            # Checklist validation is legacy; keep non-blocking.
            result.checklist_validation = _validate_checklist(parsed_obj, trigger_context if isinstance(trigger_context, dict) else {})
        else:
            logger.warning("AGENT_CHECKLIST | MISSING — could not re-parse raw_response JSON for checklist validation")
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
