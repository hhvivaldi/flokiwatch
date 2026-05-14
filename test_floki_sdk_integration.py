"""Smoke test for `floki_agent_sdk_path.decide_via_agent_sdk`.

Mocks AgentTools with the 5 most-called methods + submit_decision flow.
Verifies the SDK path:
  - imports cleanly with Floki's real system prompt
  - runs the @tool factory without binding errors
  - terminates on submit_decision and captures args
  - returns the dict shape `_parse_response_with_retry` consumes
"""

import asyncio
import os
import sys
from typing import Any, Dict, List

from dotenv import load_dotenv

load_dotenv()
os.environ.pop("ANTHROPIC_API_KEY", None)  # subscription auth


class MockAgentTools:
    def __init__(self):
        self.calls: List[str] = []
        self._chart_images: Dict[str, str] = {}  # empty — no charts in this smoke

    def get_current_price(self) -> Dict[str, Any]:
        self.calls.append("get_current_price")
        return {"bid": 4241.85, "ask": 4242.15, "spread": 0.30}

    def get_indicators(self, timeframe: str = "M15") -> Dict[str, Any]:
        self.calls.append(f"get_indicators({timeframe})")
        return {"rsi": 58.4, "macd": {"hist": 0.67}, "ema20": 4238.10, "atr14": 4.20, "tf": timeframe}

    def get_sr_zones(self, timeframe: str = "M15") -> Dict[str, Any]:
        self.calls.append(f"get_sr_zones({timeframe})")
        return {"supports": [4238.5, 4232.1], "resistances": [4248.2], "tf": timeframe}

    def get_open_positions(self) -> Dict[str, Any]:
        self.calls.append("get_open_positions")
        return {"count": 0, "positions": []}

    def get_account_info(self) -> Dict[str, Any]:
        self.calls.append("get_account_info")
        return {"balance": 10000.0, "equity": 10000.0, "free_margin": 9500.0}


MOCK_SCHEMAS = [
    {"name": "get_current_price", "description": "Return current XAU/USD bid/ask/spread.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "get_indicators", "description": "Technical indicators for a timeframe.",
     "input_schema": {"type": "object", "properties": {
         "timeframe": {"type": "string", "description": "M5/M15/H1/H4/D1"}
     }, "required": []}},
    {"name": "get_sr_zones", "description": "Support/resistance zones for a timeframe.",
     "input_schema": {"type": "object", "properties": {
         "timeframe": {"type": "string"}
     }, "required": []}},
    {"name": "get_open_positions", "description": "Currently open positions.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "get_account_info", "description": "Account balance/equity/margin.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
]


async def main():
    # Use Floki's real SYSTEM_PROMPT + SUBMIT_DECISION_TOOL — that's the actual
    # contract the SDK path has to honour.
    from agent_prompts import get_system_prompt
    from ai_agent import SUBMIT_DECISION_TOOL
    from floki_agent_sdk_path import decide_via_agent_sdk

    sys_prompt = get_system_prompt()
    print(f"[smoke] system_prompt chars: {len(sys_prompt)}")

    tools = MockAgentTools()

    user_msg = (
        "Trigger: scheduled cycle. Brain score 62 (BUY tilt). Regime: TRENDING_BULL.\n"
        "Time: 2026-05-14T13:00:00Z. No open positions.\n\n"
        "Gather minimal data (price, M15 indicators, SR zones, positions, account) "
        "then call submit_decision with decision=WAIT and a brief reasoning. "
        "This is a mock smoke test — do not place trades."
    )

    response = await decide_via_agent_sdk(
        system_prompt=sys_prompt,
        user_message=user_msg,
        instance=tools,
        schemas=MOCK_SCHEMAS,
        submit_decision_schema=SUBMIT_DECISION_TOOL,
        model="claude-opus-4-6",
        timeout=300.0,
        max_turns=20,
    )

    print(f"\n[smoke] === RESPONSE SHAPE ===")
    print(f"[smoke]   keys:                   {sorted(response.keys())}")
    print(f"[smoke]   content len:            {len(response.get('content', ''))}")
    print(f"[smoke]   input_tokens:           {response.get('input_tokens')}")
    print(f"[smoke]   output_tokens:          {response.get('output_tokens')}")
    print(f"[smoke]   model:                  {response.get('model')}")
    print(f"[smoke]   tool_trace len:         {len(response.get('tool_trace', []))}")
    print(f"[smoke]   _sdk_submit_called:     {response.get('_sdk_submit_called')}")
    print(f"[smoke]   _sdk_elapsed_s:         {response.get('_sdk_elapsed_s', 0):.2f}")
    print(f"[smoke]   _sdk_usage:             {response.get('_sdk_usage')}")

    print(f"\n[smoke] === MOCK CALL LOG ===")
    for c in tools.calls:
        print(f"[smoke]   {c}")

    print(f"\n[smoke] === CONTENT (first 600 chars) ===")
    print(response.get("content", "")[:600])

    # PASS criteria
    import json
    has_required_keys = all(k in response for k in ("content", "input_tokens", "output_tokens", "model", "tool_trace"))
    submit_called = bool(response.get("_sdk_submit_called"))
    content_parses_as_json = False
    decision_label = None
    try:
        parsed = json.loads(response.get("content", "{}"))
        content_parses_as_json = isinstance(parsed, dict)
        decision_label = parsed.get("decision")
    except Exception:
        pass

    print(f"\n[smoke] === PASS CRITERIA ===")
    print(f"[smoke]   response has required keys:           {has_required_keys}")
    print(f"[smoke]   submit_decision was called:           {submit_called}")
    print(f"[smoke]   content parses as JSON dict:          {content_parses_as_json}")
    print(f"[smoke]   decision label:                       {decision_label!r}")
    print(f"[smoke]   tool_trace populated:                 {len(response.get('tool_trace', [])) > 0}")

    overall = has_required_keys and submit_called and content_parses_as_json
    print(f"\n[smoke]   OVERALL: {'PASS' if overall else 'FAIL'}")
    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
