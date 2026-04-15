"""
LUNA DEEP SEARCH — Gemini + Google Search Grounding (FLO-236)

Searches Google for analyst articles about XAU/USD gold, returns structured
insights that feed into Luna's normal analysis cycle. Runs on startup and
every 2 hours. Cache in data/deep_research_cache.json.

Cost: ~$0.00-0.50/day (Gemini 2.5 Flash + 500 RPD free Google Search tier).
"""

import json
import os
import time
from datetime import datetime, timezone
from tz_utils import utc_iso  # FLO-309
from pathlib import Path
from typing import Any, Dict, Optional

from logger import log

DATA_DIR = Path(__file__).parent / "data"
CACHE_FILE = DATA_DIR / "deep_research_cache.json"

DEEP_SEARCH_MODEL = "gemini-2.5-flash"
SEARCH_COOLDOWN_HOURS = 2
CACHE_MAX_AGE_HOURS = 3

_SYSTEM_INSTRUCTION = (
    "You are a gold market research analyst. Search for and analyze the most recent "
    "articles and analysis about XAU/USD gold price. Focus on TODAY's analysis.\n\n"
    "Return ONLY valid JSON with these fields:\n"
    '- "analyst_consensus": one of "BULLISH", "NEUTRAL_TO_BULLISH", "NEUTRAL", '
    '"NEUTRAL_TO_BEARISH", "BEARISH"\n'
    '- "key_insight": 1-2 sentences summarizing the most important finding\n'
    '- "price_targets": {"support": [numbers], "resistance": [numbers]}\n'
    '- "risks_this_week": array of upcoming risk events (max 5)\n'
    '- "sources": array of source/publication names (max 5)\n'
    "Focus on actionable insights, not general commentary."
)


def _build_search_query() -> str:
    """Build a dynamic search query based on current market state."""
    today = datetime.now(timezone.utc).strftime("%B %d %Y")

    try:
        bs_path = DATA_DIR / "bot_state.json"
        if bs_path.exists():
            bs = json.loads(bs_path.read_text(encoding="utf-8"))
            change = bs.get("price_daily_change_pct")
            regime = str(bs.get("market_regime", {}).get("regime", "")).upper()

            # Core query based on price direction
            if change is not None and change < -0.15:
                base = f"why is gold XAU/USD falling today {today}"
            elif change is not None and change > 0.15:
                base = f"what is driving gold XAU/USD rally today {today}"
            else:
                base = f"gold XAU/USD outlook today key levels support resistance {today}"

            # Context modifiers
            try:
                luna_path = DATA_DIR / "luna_brief.json"
                if luna_path.exists():
                    lb = json.loads(luna_path.read_text(encoding="utf-8"))
                    luna_bias = str(lb.get("directional_bias", "")).upper()
                    if luna_bias == "BULLISH" and change is not None and change < 0:
                        base += " despite bullish macro"
            except Exception:
                pass

            if "RANGING" in regime:
                base += " range consolidation"
            elif "VOLATILE" in regime or "EXTREME" in regime:
                base += " high volatility"

            try:
                vix_val = bs.get("market_context", {}).get("futures", {}).get("VIX_J6", {}).get("bid")
                if vix_val is not None and float(vix_val) > 25:
                    base += " elevated market fear"
            except Exception:
                pass

            return base
    except Exception:
        pass

    return f"gold XAU/USD outlook today key levels support resistance {today}"


def _check_cache_fresh() -> bool:
    """Return True if cache exists and is younger than SEARCH_COOLDOWN_HOURS."""
    try:
        if not CACHE_FILE.exists():
            return False
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        ts = data.get("timestamp")
        if not ts:
            return False
        cache_time = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if cache_time.tzinfo is None:
            cache_time = cache_time.replace(tzinfo=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - cache_time).total_seconds() / 3600
        return age_hours < SEARCH_COOLDOWN_HOURS
    except Exception:
        return False


def load_deep_research() -> Optional[Dict[str, Any]]:
    """Load cached deep research if fresh (< CACHE_MAX_AGE_HOURS). Returns None if stale/missing."""
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
        age_hours = (datetime.now(timezone.utc) - cache_time).total_seconds() / 3600
        if age_hours > CACHE_MAX_AGE_HOURS:
            return None
        return data
    except Exception:
        return None


def run_deep_search() -> Optional[Dict[str, Any]]:
    """Run Gemini + Google Search Grounding for gold analyst research. Saves to cache."""
    try:
        import config
        if not getattr(config, "DEEP_SEARCH_ENABLED", True):
            log.debug("LUNA_DEEP | disabled via config")
            return None
    except Exception:
        pass

    if _check_cache_fresh():
        try:
            age_min = (datetime.now(timezone.utc) - datetime.fromisoformat(
                json.loads(CACHE_FILE.read_text(encoding="utf-8")).get("timestamp", "").replace("Z", "+00:00")
            ).replace(tzinfo=timezone.utc)).total_seconds() / 60
            log.info(f"LUNA_DEEP | cache fresh ({int(age_min)}m old), skipping search")
        except Exception:
            log.info("LUNA_DEEP | cache fresh, skipping search")
        return load_deep_research()

    api_key = os.environ.get("SAGE_API_KEY") or os.environ.get("GEMINI_API_KEY") or ""
    if not api_key:
        log.warning("LUNA_DEEP | no Gemini API key configured, skipping")
        return None

    query = _build_search_query()
    log.info(f"LUNA_DEEP | searching Google: {query[:80]}")

    t0 = time.time()
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key, http_options={"timeout": 60_000})
        response = client.models.generate_content(
            model=DEEP_SEARCH_MODEL,
            contents=query,
            config=types.GenerateContentConfig(
                system_instruction=_SYSTEM_INSTRUCTION,
                tools=[types.Tool(google_search=types.GoogleSearch())],
                max_output_tokens=4096,
                temperature=0.7,
            ),
        )

        latency_ms = int((time.time() - t0) * 1000)

        # Diagnostic: log finish_reason and parts count
        try:
            _cand = response.candidates[0] if response.candidates else None
            _fr = getattr(_cand, "finish_reason", "?") if _cand else "NO_CANDIDATES"
            _parts_count = len(_cand.content.parts) if _cand and _cand.content and _cand.content.parts else 0
            _has_grounding = bool(getattr(_cand, "grounding_metadata", None)) if _cand else False
            log.debug(f"LUNA_DEEP | finish_reason={_fr} | parts={_parts_count} | grounding={_has_grounding} | {latency_ms}ms")
        except Exception as _diag_e:
            log.debug(f"LUNA_DEEP | diagnostic failed: {_diag_e}")

        # response.text may fail if response has multiple parts; use manual extraction
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
        log.debug(f"LUNA_DEEP | raw response ({len(raw)} chars): {raw[:200]}")
        if not raw:
            log.warning(f"LUNA_DEEP | Gemini returned empty response ({latency_ms}ms)")
            return None

        # Detect grounding bypass — model responding from knowledge instead of searching
        # Markdown-fenced JSON (```json ... ```) is normal — only flag true plain text
        _stripped = raw.strip().lstrip("`").lstrip("json").lstrip("\n").strip()
        if raw and not _stripped.startswith("{"):
            log.warning(f"LUNA_DEEP | GROUNDING_BYPASS — model returned plain text instead of JSON ({latency_ms}ms): {raw[:150]}")

        # Extract JSON object from response (may be wrapped in markdown fences)
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            # Retry once with stronger JSON instruction
            log.warning(f"LUNA_DEEP | no JSON — retrying with strict instruction ({latency_ms}ms)")
            try:
                retry_resp = client.models.generate_content(
                    model=DEEP_SEARCH_MODEL,
                    contents=f"Based on your analysis: {raw[:500]}\n\nRespond with ONLY a valid JSON object. No prose, no markdown. Start with {{ end with }}.",
                    config=types.GenerateContentConfig(
                        system_instruction=_SYSTEM_INSTRUCTION,
                        max_output_tokens=4096,
                        temperature=0.3,
                    ),
                )
                retry_raw = ""
                try:
                    retry_raw = retry_resp.text or ""
                except Exception:
                    pass
                start = retry_raw.find("{")
                end = retry_raw.rfind("}")
                if start >= 0 and end > start:
                    raw = retry_raw
                    log.info(f"LUNA_DEEP | JSON retry succeeded ({len(retry_raw)} chars)")
                else:
                    log.warning(f"LUNA_DEEP | JSON retry also failed — no JSON in retry response")
                    return None
            except Exception as e_retry:
                log.warning(f"LUNA_DEEP | JSON retry error: {e_retry}")
                return None
        clean = raw[start:end + 1]

        parsed = None
        try:
            parsed = json.loads(clean)
        except json.JSONDecodeError:
            # Attempt repair: remove trailing commas before } or ]
            import re
            repaired = re.sub(r",\s*([}\]])", r"\1", clean)
            try:
                parsed = json.loads(repaired)
                log.debug("LUNA_DEEP | JSON repaired (trailing comma fix)")
            except json.JSONDecodeError as e2:
                log.warning(f"LUNA_DEEP | JSON parse failed ({latency_ms}ms): {e2} | raw: {raw[:300]}")
                return None

        if not isinstance(parsed, dict):
            log.warning(f"LUNA_DEEP | Gemini returned non-dict: {clean[:200]}")
            return None

        # Extract grounding metadata if available
        grounding_sources = []
        web_search_queries = []
        try:
            for candidate in (response.candidates or []):
                gm = getattr(candidate, "grounding_metadata", None)
                if gm:
                    # Web search queries used by Gemini
                    wsq = getattr(gm, "web_search_queries", None)
                    if wsq:
                        web_search_queries = list(wsq)
                    # Grounding chunks (article URLs — may not be available in all API versions)
                    for chunk in (getattr(gm, "grounding_chunks", None) or []):
                        web = getattr(chunk, "web", None)
                        if web:
                            grounding_sources.append({
                                "title": getattr(web, "title", ""),
                                "uri": getattr(web, "uri", ""),
                            })
        except Exception:
            pass

        # Build cache payload
        result = {
            "timestamp": utc_iso(),  # FLO-309
            "analyst_consensus": parsed.get("analyst_consensus", "NEUTRAL"),
            "key_insight": parsed.get("key_insight", ""),
            "price_targets": parsed.get("price_targets", {}),
            "risks_this_week": parsed.get("risks_this_week", []),
            "sources": parsed.get("sources", []),
            "search_query": query,
            "model": DEEP_SEARCH_MODEL,
            "latency_ms": latency_ms,
            "grounding_sources": grounding_sources[:10],
            "web_search_queries": web_search_queries[:5],
        }

        # Atomic write
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = str(CACHE_FILE) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        os.replace(tmp, str(CACHE_FILE))

        n_sources = len(result["sources"])
        consensus = result["analyst_consensus"]
        insight_preview = result["key_insight"][:80]
        n_grounding = len(grounding_sources)
        log.info(
            f"LUNA_DEEP | OK | {latency_ms}ms | {n_sources} sources | "
            f"{n_grounding} grounding refs | consensus: {consensus} | {insight_preview}"
        )
        return result

    except json.JSONDecodeError as e:
        latency_ms = int((time.time() - t0) * 1000)
        log.warning(f"LUNA_DEEP | JSON parse error ({latency_ms}ms): {e}")
        return None
    except Exception as e:
        latency_ms = int((time.time() - t0) * 1000)
        log.warning(f"LUNA_DEEP | error ({latency_ms}ms): {e}")
        return None
