"""
AI AGENT - Claude-based Trading Decision Maker
The Agent receives market data, Brain analysis, and makes independent trading decisions.
Agent is the decision maker and executor.
"""

import json
import asyncio
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional, List, Any
from enum import Enum

from logger import log
from agent_prompts import get_system_prompt, get_prompt_hash, get_prompt_version

logger = log


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
        self.timeout = 30
        self.enabled = False
        self.mode = "shadow"  # shadow | gate | full
        self._initialized = False

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
            self.timeout = getattr(config, 'AI_AGENT_TIMEOUT', 30)
            self.mode = getattr(config, 'AI_AGENT_MODE', 'shadow')
            
            self._initialized = True
            logger.info(f"AI Agent initialized: model={self.model}, mode={self.mode}, timeout={self.timeout}s")
            return True
            
        except Exception as e:
            logger.error(f"Failed to initialize AI Agent: {e}")
            self.enabled = False
            return False

    async def decide(self, data_package: Dict, trigger_type: str = "SIGNAL") -> AgentResult:
        """
        Make a trading decision based on the data package.
        
        Args:
            data_package: Complete market context (built by agent_data_builder)
            
        Returns:
            AgentResult with decision and reasoning
        """
        if not self.enabled or not self._initialized:
            return self._fallback_result("Agent not enabled or not initialized")
        
        start_time = datetime.utcnow()
        
        try:
            # Build the user message with the data package
            user_message = self._build_user_message(data_package, trigger_type=trigger_type)
            
            # Call Claude API with timeout
            response = await asyncio.wait_for(
                self._call_claude(user_message),
                timeout=self.timeout
            )
            
            # Calculate latency
            latency_ms = int((datetime.utcnow() - start_time).total_seconds() * 1000)
            
            # Parse the response
            result = self._parse_response(response, latency_ms)
            
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

    async def _call_claude(self, user_message: str) -> Dict:
        """
        Call the Claude API.
        
        Args:
            user_message: The formatted user message with data package
            
        Returns:
            API response dict
        """
        # Run in executor since anthropic client is sync
        loop = asyncio.get_event_loop()
        
        def _sync_call():
            return self.client.messages.create(
                model=self.model,
                max_tokens=1024,
                system=get_system_prompt(),
                messages=[
                    {"role": "user", "content": user_message}
                ]
            )
        
        response = await loop.run_in_executor(None, _sync_call)
        
        return {
            "content": response.content[0].text if response.content else "",
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "model": response.model,
        }

    def _build_user_message(self, data_package: Dict, trigger_type: str = "SIGNAL") -> str:
        """
        Build the user message from the data package.
        
        Args:
            data_package: Complete market context
            
        Returns:
            Formatted user message string
        """
        proactive_xml = None
        if trigger_type == "PROACTIVE_H1":
            try:
                from agent_data_builder import format_proactive_xml
                proactive_xml = format_proactive_xml(data_package)
            except Exception as e:
                logger.warning(f"Failed to format proactive XML, falling back to JSON: {e}")

        # Format the data package as readable JSON (default / fallback)
        formatted_data = json.dumps(data_package, indent=2, default=str)
        
        # Get Brain's signal for context
        brain = data_package.get("brain_analysis", {})
        brain_decision = brain.get("decision", "HOLD")
        brain_score = brain.get("score", 50)
        brain_confidence = brain.get("confidence", 50)
        
        # Build memory context section if present
        memory_section = ""
        memory_context = data_package.get("agent_memory_context")
        if memory_context and memory_context.get("has_previous_reject"):
            all_met = memory_context.get("all_conditions_met", False)
            conditions_str = "\n".join(memory_context.get("conditions_status", []))
            time_remaining = memory_context.get("invalidation", {}).get("time_remaining", "unknown")
            
            if all_met:
                memory_section = f"""

## ⚠️ PREVIOUS REJECT CONTEXT — ALL CONDITIONS NOW MET

In your previous cycle, you REJECTED a {memory_context.get('brain_signal_rejected')} signal.

Your market view was: **{memory_context.get('your_market_view', {}).get('direction', 'N/A')}**
"{memory_context.get('your_market_view', {}).get('description', 'N/A')}"

Your conditions to approve:
{conditions_str}

**All conditions from your previous REJECT are now met.**

Time remaining before invalidation: {time_remaining}

You should either:
1. APPROVE the trade (OPEN_BUY or OPEN_SELL) if the setup is now valid
2. Explain clearly why you are still rejecting despite conditions being met
"""
            else:
                memory_section = f"""

## PREVIOUS REJECT CONTEXT

In your previous cycle, you REJECTED a {memory_context.get('brain_signal_rejected')} signal.

Your market view was: **{memory_context.get('your_market_view', {}).get('direction', 'N/A')}**
"{memory_context.get('your_market_view', {}).get('description', 'N/A')}"

Your conditions to approve:
{conditions_str}

Time remaining before invalidation: {time_remaining}

Maintain consistency with your previous analysis unless market conditions have materially changed.
"""
        
        header_line = ""
        if trigger_type == "PROACTIVE_H1":
            header_line = "This is your independent H1 market snapshot. Analyze the raw market data below and provide YOUR trading view. What does the price structure tell you? What would YOU trade right now? Respond with OPEN_BUY, OPEN_SELL, or WAIT only."
        else:
            header_line = f"The Brain has signaled: **{brain_decision}** (score: {brain_score:.1f}, confidence: {brain_confidence:.0f}%)"

        if trigger_type == "PROACTIVE_H1" and proactive_xml:
            message = proactive_xml
        else:
            message = f"""## CURRENT MARKET DATA

{header_line}
{memory_section}
Review the complete context below and make your decision.

```json
{formatted_data}
```

Based on this data, what is your decision? Remember to evaluate the CONTEXT, not just the numbers. Respond with valid JSON."""

        return message

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
    data_package: Dict,
    trigger_type: str = "SIGNAL",
    allow_memory_write: bool = True,
) -> AgentResult:
    """
    Convenience function to get Agent decision.
    Handles memory injection and saving.
    
    Args:
        data_package: Complete market context
        
    Returns:
        AgentResult with decision
    """
    agent = get_agent()
    
    # Inject memory context into data package (v1.3)
    if trigger_type != "PROACTIVE_H1":
        try:
            from agent_memory import get_memory_context_for_agent
            memory_context = get_memory_context_for_agent()
            if memory_context:
                data_package["agent_memory_context"] = memory_context
                logger.debug(f"Injected memory context: all_conditions_met={memory_context.get('all_conditions_met')}")
        except Exception as e:
            logger.warning(f"Failed to inject memory context: {e}")
    
    # Get Agent decision
    result = await agent.decide(data_package, trigger_type=trigger_type)
    
    # Save REJECT to memory (v1.3)
    if allow_memory_write and result.decision == "REJECT":
        if result.market_view and result.conditions_to_approve:
            try:
                from agent_memory import save_reject
                brain = data_package.get("brain_analysis", {})
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
    result = await agent.decide(mock_data)
    print(f"   Decision: {result.decision}")
    print(f"   Error: {result.error}")
    
    # Test initialization (will fail without API key)
    print("\n2. Test initialization:")
    success = agent.initialize()
    print(f"   Initialized: {success}")
    print(f"   Enabled: {agent.is_enabled()}")
    
    if agent.is_enabled():
        print("\n3. Test with real API call:")
        result = await agent.decide(mock_data)
        print(f"   Decision: {result.decision}")
        print(f"   Confidence: {result.confidence}")
        print(f"   Reasoning: {result.reasoning[:100]}...")
        print(f"   Tokens: {result.input_tokens} in, {result.output_tokens} out")
        print(f"   Latency: {result.latency_ms}ms")


if __name__ == "__main__":
    asyncio.run(_test_agent())
