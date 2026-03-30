"""
FLO-137: Post-trade reflexion engine.
After a trade closes, gathers context and calls GPT-5.4 to extract lessons.
Runs in a daemon thread — never blocks the main loop.
"""

import json
import os
import time
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from logger import log


REFLEXION_SYSTEM_PROMPT = """You are a trade analyst reviewing a completed XAU/USD trade. Given the thesis at entry, Rex's debate, market conditions, and the actual outcome — analyze what happened.

Return JSON only:
{
  "was_thesis_correct": true/false,
  "what_actually_happened": "1-2 sentences on price action",
  "lesson": "1 sentence — the key takeaway for future trades",
  "pattern_tags": ["tag1", "tag2"],
  "confidence_calibration": "overconfident/accurate/underconfident",
  "would_take_again": true/false,
  "what_would_change": "1 sentence or null"
}

pattern_tags: use lowercase snake_case. Examples: false_breakout, trend_continuation, news_reversal, sl_too_tight, tp_too_ambitious, good_entry_bad_exit, round_number_rejection, asian_session_trap."""


def _build_user_prompt(action: Dict, conditions: Dict, agent_row: Optional[Dict]) -> str:
    """Build the user prompt with all available trade context."""
    thesis = conditions.get("thesis_at_open") or {}
    rex = conditions.get("rex_at_open") or {}

    parts = []
    parts.append(f"TRADE: {action.get('direction', '?')} XAU/USD")
    parts.append(f"Entry: {action.get('open_price', '?')} | Exit: {action.get('close_price', '?')}")
    parts.append(f"P&L: ${action.get('profit', 0):.2f} | Close reason: {action.get('reason', '?')}")

    if thesis:
        parts.append(f"\nTHESIS AT ENTRY:")
        parts.append(f"Direction bias: {thesis.get('direction_bias', '?')}")
        parts.append(f"Conditions: {thesis.get('conditions', '?')}")
        parts.append(f"Key levels: {thesis.get('key_levels', '?')}")

    if rex:
        parts.append(f"\nREX DEBATE:")
        parts.append(f"Agreed: {rex.get('agree', '?')}")
        reasoning = rex.get("reasoning", "")
        if reasoning:
            parts.append(f"Rex said: {reasoning[:500]}")

    if agent_row:
        parts.append(f"\nAGENT REASONING AT ENTRY:")
        parts.append(f"{(agent_row.get('agent_reasoning') or '')[:500]}")
        concerns = agent_row.get("agent_concerns", "")
        if concerns:
            parts.append(f"Concerns: {concerns[:300]}")

    # Indicator snapshot
    for key in ("rsi_h1", "adx_h1", "atr_h1", "session", "luna_environment", "luna_bias"):
        val = conditions.get(key)
        if val is not None:
            parts.append(f"{key}: {val}")

    return "\n".join(parts)


def _load_trade_conditions(ticket: int) -> Dict:
    """Load trade_conditions/{ticket}.json if it exists."""
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "trade_conditions", f"{ticket}.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("conditions_at_open", {})
    except Exception as e:
        log.debug(f"REFLEXION | failed to load conditions for ticket {ticket}: {e}")
    return {}


def _get_agent_decision_row(ticket: int, open_time: str) -> Optional[Dict]:
    """Find the agent_decisions row closest to the trade open time."""
    try:
        from db_writer import get_agent_decision_near_time
        return get_agent_decision_near_time(open_time)
    except Exception as e:
        log.debug(f"REFLEXION | failed to query agent_decisions: {e}")
    return None


def _call_reflexion_llm(system: str, user: str) -> Dict:
    """Call GPT-5.4 for reflexion analysis. Returns parsed JSON + metadata."""
    import config
    from openai import OpenAI

    model = getattr(config, "FLOKI_MODEL", "gpt-4o")
    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", getattr(config, "OPENAI_API_KEY", "")))

    start = time.time()
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.3,
        max_completion_tokens=500,
        response_format={"type": "json_object"},
        timeout=30,
    )
    latency_ms = int((time.time() - start) * 1000)

    text = resp.choices[0].message.content or "{}"
    # Strip markdown fences
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text[:-3].strip()

    tokens = (resp.usage.prompt_tokens + resp.usage.completion_tokens) if resp.usage else 0

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = {"lesson": "reflexion_parse_error", "pattern_tags": []}

    return {"parsed": parsed, "raw": text, "model": model, "tokens": tokens, "latency_ms": latency_ms}


def run_trade_reflexion(action: Dict) -> None:
    """Main entry point — runs the full reflexion pipeline for a closed trade.
    Called in a daemon thread from main.py."""
    ticket = action.get("ticket")
    if not ticket:
        return

    try:
        log.info(f"REFLEXION | starting for ticket={ticket}")

        # Gather context
        conditions = _load_trade_conditions(ticket)
        open_time = action.get("open_time") or conditions.get("open_time", "")
        agent_row = _get_agent_decision_row(ticket, open_time) if open_time else None

        user_prompt = _build_user_prompt(action, conditions, agent_row)

        # Call LLM
        result = _call_reflexion_llm(REFLEXION_SYSTEM_PROMPT, user_prompt)
        parsed = result["parsed"]

        # Extract fields
        lesson = parsed.get("lesson", "")
        tags = parsed.get("pattern_tags", [])
        thesis_summary = ""
        thesis = conditions.get("thesis_at_open", {})
        if thesis:
            thesis_summary = f"{thesis.get('direction_bias', '?')}: {thesis.get('conditions', '')}"[:500]

        # Store in DB
        from db_writer import record_trade_reflexion
        record_trade_reflexion(
            ticket=ticket,
            direction=action.get("direction", ""),
            entry_price=action.get("open_price", 0),
            exit_price=action.get("close_price", 0),
            pnl=action.get("profit", 0),
            close_reason=action.get("reason", ""),
            thesis_summary=thesis_summary,
            reflexion_json=result["raw"],
            lesson=lesson,
            pattern_tags=json.dumps(tags),
            model=result["model"],
            tokens=result["tokens"],
            latency_ms=result["latency_ms"],
        )

        log.info(f"REFLEXION | ticket={ticket} | lesson={lesson[:80]} | tags={tags}")

        # FLO-138 Phase 2: embed into ChromaDB for semantic search
        try:
            embed_text = f"{lesson} {' '.join(tags)} {thesis_summary}"
            _embed_reflexion(ticket, embed_text, {
                "ticket": ticket,
                "direction": action.get("direction", ""),
                "pnl": action.get("profit", 0),
                "lesson": lesson,
                "pattern_tags": json.dumps(tags),
            })
        except Exception as e:
            log.debug(f"REFLEXION | ChromaDB embed failed for ticket={ticket}: {e}")

    except Exception as e:
        log.warning(f"REFLEXION | failed for ticket={ticket}: {e}")


# -------------------------------------------------------------------------
# FLO-138 Phase 2: ChromaDB semantic memory
# -------------------------------------------------------------------------

_CHROMA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "chromadb")
_COLLECTION_NAME = "trade_reflexions"
_chroma_client = None
_chroma_collection = None
_chroma_lock = threading.Lock()


def _get_chroma_collection():
    """Lazy-init ChromaDB persistent client + collection (thread-safe)."""
    global _chroma_client, _chroma_collection
    if _chroma_collection is not None:
        return _chroma_collection
    with _chroma_lock:
        if _chroma_collection is not None:
            return _chroma_collection
        try:
            import chromadb
            os.makedirs(_CHROMA_DIR, exist_ok=True)
            _chroma_client = chromadb.PersistentClient(path=_CHROMA_DIR)
            _chroma_collection = _chroma_client.get_or_create_collection(
                name=_COLLECTION_NAME,
                metadata={"hnsw:space": "cosine"},
            )
            return _chroma_collection
        except Exception as e:
            log.debug(f"REFLEXION | ChromaDB init failed: {e}")
            return None


_embed_client = None


def _get_embed_client():
    """Cached OpenAI client for embeddings."""
    global _embed_client
    if _embed_client is not None:
        return _embed_client
    import config
    from openai import OpenAI
    _embed_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", getattr(config, "OPENAI_API_KEY", "")))
    return _embed_client


def _get_embedding(text: str) -> Optional[list]:
    """Get embedding from OpenAI text-embedding-3-small."""
    try:
        client = _get_embed_client()
        resp = client.embeddings.create(
            model="text-embedding-3-small",
            input=text[:8000],
        )
        return resp.data[0].embedding
    except Exception as e:
        log.debug(f"REFLEXION | embedding failed: {e}")
        return None


def _embed_reflexion(ticket: int, text: str, metadata: Dict) -> bool:
    """Embed a single reflexion into ChromaDB."""
    col = _get_chroma_collection()
    if col is None:
        return False
    embedding = _get_embedding(text)
    if embedding is None:
        return False
    doc_id = f"ticket_{ticket}"
    # Upsert — handles both new and re-embed
    # ChromaDB supports str, int, float natively — preserve types
    safe_meta = {}
    for k, v in metadata.items():
        if isinstance(v, (str, int, float, bool)):
            safe_meta[k] = v
        else:
            safe_meta[k] = str(v)
    col.upsert(
        ids=[doc_id],
        embeddings=[embedding],
        documents=[text],
        metadatas=[safe_meta],
    )
    log.debug(f"REFLEXION | embedded ticket={ticket} into ChromaDB")
    return True


def search_memory(query: str, limit: int = 3) -> list:
    """Semantic search across trade reflexions using ChromaDB.
    Returns list of dicts with ticket, lesson, pnl, distance."""
    col = _get_chroma_collection()
    if col is None:
        return []
    embedding = _get_embedding(query)
    if embedding is None:
        return []
    try:
        results = col.query(
            query_embeddings=[embedding],
            n_results=min(limit, 20),
            include=["documents", "metadatas", "distances"],
        )
        out = []
        for i, doc_id in enumerate(results["ids"][0]):
            meta = results["metadatas"][0][i] if results["metadatas"] else {}
            out.append({
                "ticket": meta.get("ticket", "?"),
                "lesson": meta.get("lesson", ""),
                "pnl": meta.get("pnl", "?"),
                "direction": meta.get("direction", "?"),
                "pattern_tags": json.loads(meta.get("pattern_tags", "[]")),
                "similarity": round(1.0 - float(results["distances"][0][i]), 3),
                "text": results["documents"][0][i] if results["documents"] else "",
            })
        return out
    except Exception as e:
        log.debug(f"REFLEXION | ChromaDB search failed: {e}")
        return []


def sync_chromadb_on_startup() -> int:
    """Re-embed any reflexions in SQLite not yet in ChromaDB. Call at startup."""
    col = _get_chroma_collection()
    if col is None:
        return 0
    try:
        from db_writer import get_recent_reflexions
        all_refs = get_recent_reflexions(limit=500)
        existing_ids = set()
        try:
            existing = col.get(include=[])
            existing_ids = set(existing["ids"])
        except Exception:
            pass

        added = 0
        for ref in all_refs:
            doc_id = f"ticket_{ref['ticket']}"
            if doc_id in existing_ids:
                continue
            text = f"{ref.get('lesson', '')} {' '.join(ref.get('pattern_tags', []))} {ref.get('thesis_summary', '')}"
            meta = {
                "ticket": ref["ticket"],
                "direction": ref.get("direction", ""),
                "pnl": ref.get("pnl", 0),
                "lesson": ref.get("lesson", ""),
                "pattern_tags": json.dumps(ref.get("pattern_tags", [])),
            }
            if _embed_reflexion(ref["ticket"], text, meta):
                added += 1
        if added > 0:
            log.info(f"REFLEXION | ChromaDB startup sync: embedded {added} missing reflexions")
        return added
    except Exception as e:
        log.debug(f"REFLEXION | ChromaDB startup sync failed: {e}")
        return 0


def run_trade_reflexion_async(action: Dict) -> None:
    """Launch reflexion in a daemon thread. Never blocks the main loop."""
    t = threading.Thread(target=run_trade_reflexion, args=(action,), daemon=True)
    t.start()


# -------------------------------------------------------------------------
# FLO-147: Delayed hindsight analysis (runs 1-2h after trade close)
# -------------------------------------------------------------------------

HINDSIGHT_DELAY_SECONDS = 3600  # 1 hour
HINDSIGHT_SYSTEM_PROMPT = """You are reviewing a completed trade WITH hindsight — you can see what price did AFTER the trade closed.

Given: the original trade details, the original reflexion lesson, and the post-close price action — revise the lesson.

Return JSON only:
{
  "original_lesson_correct": true/false,
  "revised_lesson": "1-2 sentences — the corrected takeaway given what actually happened",
  "would_original_sl_have_survived": true/false,
  "would_tp_have_been_hit": true/false,
  "post_close_move_pips": number,
  "hindsight_tags": ["tag1", "tag2"]
}

hindsight_tags: lowercase snake_case. Examples: sl_tightening_saved_money, sl_tightening_cost_money, thesis_correct_bad_execution, thesis_wrong, patience_would_have_paid."""


def _get_post_close_prices(close_time_str: str, direction: str, entry_price: float) -> Optional[Dict]:
    """Fetch price at close+1h from MT5."""
    try:
        import MetaTrader5 as mt5
        from datetime import timedelta

        if not mt5.initialize():
            return None

        close_dt = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
        if close_dt.tzinfo is None:
            close_dt = close_dt.replace(tzinfo=timezone.utc)

        target_1h = close_dt + timedelta(hours=1)
        bars = mt5.copy_rates_range("XAUUSD", mt5.TIMEFRAME_M5, close_dt, target_1h)
        mt5.shutdown()

        if bars is None or len(bars) == 0:
            return None

        import numpy as np
        highs = [float(b[2]) for b in bars]  # high
        lows = [float(b[3]) for b in bars]   # low
        closes = [float(b[4]) for b in bars]  # close

        last_close = closes[-1]
        post_high = max(highs)
        post_low = min(lows)

        if direction == "BUY":
            mfe_post = (post_high - entry_price) / 0.1
            mae_post = (entry_price - post_low) / 0.1
            move_pips = (last_close - entry_price) / 0.1
        else:
            mfe_post = (entry_price - post_low) / 0.1
            mae_post = (post_high - entry_price) / 0.1
            move_pips = (entry_price - last_close) / 0.1

        return {
            "price_at_1h": last_close,
            "post_high": post_high,
            "post_low": post_low,
            "move_pips": round(move_pips),
            "mfe_post": round(mfe_post),
            "mae_post": round(mae_post),
            "bars_count": len(bars),
        }
    except Exception as e:
        log.debug(f"REFLEXION | failed to get post-close prices: {e}")
        return None


def run_delayed_hindsight(ticket: int, action: Dict) -> None:
    """Run hindsight analysis for a trade. Called after HINDSIGHT_DELAY_SECONDS."""
    try:
        log.info(f"REFLEXION | hindsight starting for ticket={ticket}")

        from db_writer import get_recent_reflexions, update_reflexion_hindsight

        # Get original reflexion
        refs = get_recent_reflexions(limit=50)
        original = None
        for r in refs:
            if r.get("ticket") == ticket:
                original = r
                break

        if not original:
            log.debug(f"REFLEXION | hindsight: no reflexion found for ticket={ticket}")
            return

        # Get post-close price data
        close_time = action.get("close_time") or action.get("open_time", "")
        direction = action.get("direction", "BUY")
        entry_price = float(action.get("open_price", 0))
        exit_price = float(action.get("close_price", 0))
        original_sl = float(action.get("original_sl", 0) or 0)

        post_prices = _get_post_close_prices(close_time, direction, entry_price)
        if not post_prices:
            log.debug(f"REFLEXION | hindsight: no post-close data for ticket={ticket}")
            return

        # Check if original SL would have survived
        sl_from_conditions = None
        try:
            conditions = _load_trade_conditions(ticket)
            # Original SL is in the trade record, not conditions
        except Exception:
            pass

        would_sl_survive = True
        would_tp_hit = False
        tp = float(action.get("tp", 0) or 0)

        if direction == "BUY":
            if original_sl > 0 and post_prices["post_low"] <= original_sl:
                would_sl_survive = False
            if tp > 0 and post_prices["post_high"] >= tp:
                would_tp_hit = True
        else:
            if original_sl > 0 and post_prices["post_high"] >= original_sl:
                would_sl_survive = False
            if tp > 0 and post_prices["post_low"] <= tp:
                would_tp_hit = True

        # Build hindsight prompt
        user_prompt = (
            f"ORIGINAL TRADE: {direction} entry={entry_price} exit={exit_price} P&L=${action.get('profit', 0)}\n"
            f"Original SL: {original_sl} | TP: {tp}\n"
            f"Original lesson: {original.get('lesson', '?')}\n"
            f"Original tags: {original.get('pattern_tags', [])}\n\n"
            f"POST-CLOSE (1 hour later):\n"
            f"Price at +1h: {post_prices['price_at_1h']}\n"
            f"Post-close high: {post_prices['post_high']} | low: {post_prices['post_low']}\n"
            f"Move since entry: {post_prices['move_pips']} pips\n"
            f"Would original SL have been hit: {'YES' if not would_sl_survive else 'NO'}\n"
            f"Would TP have been hit: {'YES' if would_tp_hit else 'NO'}\n"
        )

        result = _call_reflexion_llm(HINDSIGHT_SYSTEM_PROMPT, user_prompt)
        parsed = result["parsed"]

        revised_lesson = parsed.get("revised_lesson", "")
        hindsight_data = {
            "post_prices": post_prices,
            "would_original_sl_survive": would_sl_survive,
            "would_tp_hit": would_tp_hit,
            "original_lesson_correct": parsed.get("original_lesson_correct"),
            "hindsight_tags": parsed.get("hindsight_tags", []),
            "revised_lesson": revised_lesson,
            "model": result["model"],
        }

        update_reflexion_hindsight(ticket, json.dumps(hindsight_data), revised_lesson)

        # Update ChromaDB with enriched data
        try:
            enriched_text = f"{revised_lesson} {' '.join(parsed.get('hindsight_tags', []))} {original.get('thesis_summary', '')}"
            _embed_reflexion(ticket, enriched_text, {
                "ticket": ticket,
                "direction": direction,
                "pnl": action.get("profit", 0),
                "lesson": revised_lesson,
                "pattern_tags": json.dumps(parsed.get("hindsight_tags", [])),
            })
        except Exception as e:
            log.debug(f"REFLEXION | hindsight ChromaDB update failed: {e}")

        log.info(f"REFLEXION | hindsight complete for ticket={ticket} | revised={revised_lesson[:80]}")

    except Exception as e:
        log.warning(f"REFLEXION | hindsight failed for ticket={ticket}: {e}")


def schedule_delayed_hindsight(action: Dict) -> None:
    """Schedule hindsight analysis to run after HINDSIGHT_DELAY_SECONDS."""
    ticket = action.get("ticket")
    if not ticket:
        return

    def _delayed():
        import time as _time
        _time.sleep(HINDSIGHT_DELAY_SECONDS)
        run_delayed_hindsight(ticket, action)

    t = threading.Thread(target=_delayed, daemon=True)
    t.start()
    log.info(f"REFLEXION | hindsight scheduled for ticket={ticket} in {HINDSIGHT_DELAY_SECONDS}s")
