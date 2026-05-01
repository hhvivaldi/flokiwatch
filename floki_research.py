"""FLOKI RESEARCH — Floki-specific Google-grounded search for plan-building.

Distinct from deep_search.py (Luna's 2h macro grounding):
  - Different query intent: planner-focused, not macro narrative
  - Different cache: data/floki_research_cache.json, 30-min TTL
  - Synchronous on-call: Floki pulls via get_analyst_research tool when he wants
  - Output schema is structured for plan authoring (key_levels / setups / targets),
    not regime classification (analyst_consensus / risks_this_week)

Cost: ~$0.00-0.50/day (Gemini 2.5 Flash + 500 RPD free Google Search tier);
shares free quota with deep_search.py — combined still well under cap.

CEO directive 2026-05-01 (FLO-419 Phase 2): Floki authoring needs to know what
levels other professional traders are watching for the current session, what
intraday setups they're calling out, and the analyst directional bias for today.
Macro generics ("what's driving gold") are Luna's job; this tool answers
"what levels should I build plans around?"
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from logger import log
from tz_utils import utc_iso

DATA_DIR = Path(__file__).parent / "data"
CACHE_FILE = DATA_DIR / "floki_research_cache.json"

FLOKI_RESEARCH_MODEL = "gemini-2.5-flash"
CACHE_TTL_MINUTES = 30  # matches Floki's planning cycle cadence

_SYSTEM_INSTRUCTION = (
    "You are an XAUUSD intraday planner's research assistant. Search Google for "
    "the most recent (TODAY) gold market analysis from technical analysts and "
    "financial news. Your job is to surface what other professional traders are "
    "watching FOR PLAN-BUILDING, not macro narratives.\n\n"
    "Focus on:\n"
    "  - Key support/resistance levels traders are watching for the current session\n"
    "  - Specific intraday technical analysis setups and patterns being called out\n"
    "  - Analyst price targets (numerical) and short-term directional bias for TODAY\n\n"
    "DO NOT focus on:\n"
    "  - Generic macro drivers (covered separately by another agent)\n"
    "  - Long-term outlook (>1 week)\n"
    '  - General "gold is/might be"-type commentary\n\n'
    "Return ONLY valid JSON with this exact shape:\n"
    "{\n"
    '  "key_levels": {\n'
    '    "support": [<numeric>, ...],\n'
    '    "resistance": [<numeric>, ...]\n'
    "  },\n"
    '  "setups_called_out": [\n'
    '    {"timeframe": "M15|H1|H4|D1", "pattern": "<short name>", "thesis": "<1 sentence>"}, ...\n'
    "  ],\n"
    '  "analyst_targets": {\n'
    '    "bullish_target": <number or null>,\n'
    '    "bearish_target": <number or null>,\n'
    '    "consensus_bias": "BULLISH" | "NEUTRAL_BULLISH" | "NEUTRAL" | "NEUTRAL_BEARISH" | "BEARISH"\n'
    "  },\n"
    '  "key_themes_today": [<short string>, ...],\n'
    '  "sources": [<publication>, ...]\n'
    "}\n\n"
    "Constraints: max 5 support + 5 resistance levels, max 5 setups, max 3 themes, "
    "max 5 sources. Numeric levels only (e.g. 4582.5, not '4582 zone'). If a source "
    "is older than today, exclude it. If you cannot find concrete numeric levels, "
    "leave the arrays empty rather than fabricating. No prose, no markdown fences."
)


def _build_query() -> str:
    today = datetime.now(timezone.utc).strftime("%B %d %Y")
    return (
        f"XAUUSD gold today {today} key support resistance levels watching, "
        f"intraday technical analysis setups H1 H4, analyst price targets and "
        f"directional bias for today's session"
    )


def _cache_age_minutes() -> Optional[float]:
    """Return age of cache in minutes, or None if absent / unparseable."""
    try:
        if not CACHE_FILE.exists():
            return None
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        ts = data.get("timestamp")
        if not ts:
            return None
        cache_time = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if cache_time.tzinfo is None:
            cache_time = cache_time.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - cache_time).total_seconds() / 60
    except Exception:
        return None


def load_floki_research() -> Optional[Dict[str, Any]]:
    """Return cached research if present and fresh (< CACHE_TTL_MINUTES). None otherwise."""
    age = _cache_age_minutes()
    if age is None or age >= CACHE_TTL_MINUTES:
        return None
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_cache(payload: Dict[str, Any]) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_FILE.with_suffix(CACHE_FILE.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, CACHE_FILE)
    except Exception as e:
        log.warning(f"FLOKI_RESEARCH | cache write failed: {e}")


def run_floki_research(force: bool = False) -> Optional[Dict[str, Any]]:
    """Fire Gemini + Google Search and cache. If cache is fresh (<30min) and force=False,
    return the cached payload without searching. Returns None on hard failure."""
    if not force:
        cached = load_floki_research()
        if cached is not None:
            age = _cache_age_minutes()
            log.debug(f"FLOKI_RESEARCH | cache hit ({int(age) if age else '?'}m old)")
            return cached

    try:
        import config
        if not getattr(config, "FLOKI_RESEARCH_ENABLED", True):
            log.debug("FLOKI_RESEARCH | disabled via config")
            return None
    except Exception:
        pass

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("SAGE_API_KEY") or ""
    if not api_key:
        log.warning("FLOKI_RESEARCH | no Gemini API key configured")
        return None

    query = _build_query()
    log.info(f"FLOKI_RESEARCH | searching: {query[:80]}")

    t0 = time.time()
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key, http_options={"timeout": 60_000})
        response = client.models.generate_content(
            model=FLOKI_RESEARCH_MODEL,
            contents=query,
            config=types.GenerateContentConfig(
                system_instruction=_SYSTEM_INSTRUCTION,
                tools=[types.Tool(google_search=types.GoogleSearch())],
                max_output_tokens=4096,
                temperature=0.5,
            ),
        )
        latency_ms = int((time.time() - t0) * 1000)

        raw = ""
        try:
            raw = response.text or ""
        except Exception:
            try:
                for part in (response.candidates[0].content.parts or []):
                    if hasattr(part, "text") and part.text:
                        raw += part.text
            except Exception:
                pass

        if not raw:
            log.warning(f"FLOKI_RESEARCH | empty Gemini response ({latency_ms}ms)")
            return None

        # Extract JSON object even if wrapped in markdown fences
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            log.warning(f"FLOKI_RESEARCH | no JSON in response ({latency_ms}ms): {raw[:200]}")
            return None
        clean = raw[start:end + 1]

        try:
            parsed = json.loads(clean)
        except json.JSONDecodeError:
            # Trailing-comma repair (same approach as deep_search.py)
            repaired = re.sub(r",\s*([}\]])", r"\1", clean)
            try:
                parsed = json.loads(repaired)
            except json.JSONDecodeError as e2:
                log.warning(f"FLOKI_RESEARCH | JSON parse failed ({latency_ms}ms): {e2}")
                return None

        # Wrap with metadata
        payload = {
            "timestamp": utc_iso(),
            "latency_ms": latency_ms,
            "model": FLOKI_RESEARCH_MODEL,
            "query": query,
            **parsed,
        }
        _save_cache(payload)
        log.info(
            f"FLOKI_RESEARCH | OK | "
            f"sup={len(payload.get('key_levels', {}).get('support', []))} "
            f"res={len(payload.get('key_levels', {}).get('resistance', []))} "
            f"setups={len(payload.get('setups_called_out', []))} "
            f"bias={payload.get('analyst_targets', {}).get('consensus_bias', '?')} "
            f"| {latency_ms}ms"
        )
        return payload

    except Exception as e:
        latency_ms = int((time.time() - t0) * 1000)
        log.warning(f"FLOKI_RESEARCH | search error ({latency_ms}ms): {type(e).__name__}: {e}")
        return None


def get_floki_research() -> Optional[Dict[str, Any]]:
    """Public entry point: return fresh-or-just-fetched research, None on failure."""
    return run_floki_research(force=False)
