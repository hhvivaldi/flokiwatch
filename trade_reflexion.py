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


def _build_rich_embed_text(
    lesson: str,
    tags: list,
    thesis_summary: str,
    action: Optional[Dict] = None,
    conditions: Optional[Dict] = None,
) -> str:
    """FLO-177: Build enriched embed text with full trade context for ChromaDB.
    All three embed points (close, hindsight, startup sync) use this."""
    parts = [lesson, " ".join(tags) if tags else ""]
    action = action or {}
    conditions = conditions or {}

    # Trade entry/exit context
    direction = action.get("direction", "")
    open_price = action.get("open_price") or action.get("entry_price")
    close_price = action.get("close_price") or action.get("exit_price")
    profit = action.get("profit") or action.get("pnl")
    reason = action.get("reason") or action.get("close_reason", "")
    sl = action.get("original_sl") or action.get("sl")
    tp = action.get("tp")

    if direction and open_price is not None:
        entry = f"ENTRY: {direction} at ${open_price}"
        if sl is not None:
            entry += f" SL=${sl}"
        if tp is not None:
            entry += f" TP=${tp}"
        parts.append(entry)
    if close_price is not None and profit is not None:
        parts.append(f"EXIT: ${close_price} P&L=${profit:.2f} {reason}")

    # Indicator snapshot from conditions
    rsi = conditions.get("rsi_h1")
    adx = conditions.get("adx_h1")
    atr = conditions.get("atr_h1")
    if any(v is not None for v in (rsi, adx, atr)):
        ind_parts = []
        if rsi is not None:
            ind_parts.append(f"RSI={rsi}")
        if adx is not None:
            ind_parts.append(f"ADX={adx}")
        if atr is not None:
            ind_parts.append(f"ATR={atr}")
        parts.append(f"INDICATORS: {' '.join(ind_parts)}")

    # Regime + session
    regime = conditions.get("regime")
    session = conditions.get("session")
    if regime or session:
        ctx = []
        if regime:
            ctx.append(f"REGIME: {regime}")
        if session:
            ctx.append(f"SESSION: {session}")
        parts.append(" ".join(ctx))

    # Luna context
    luna_bias = conditions.get("luna_bias")
    luna_env = conditions.get("luna_environment")
    if luna_bias or luna_env:
        luna = f"LUNA: {luna_bias or '?'}"
        if luna_env:
            luna += f" ({luna_env})"
        parts.append(luna)

    # Thesis
    if thesis_summary:
        parts.append(f"THESIS: {thesis_summary}")

    return " | ".join(p for p in parts if p)


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

    model = getattr(config, "REFLEXION_MODEL", None) or "gpt-4o-mini"
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

        # FLO-138 Phase 2 + FLO-177: embed enriched context into ChromaDB
        try:
            embed_text = _build_rich_embed_text(lesson, tags, thesis_summary, action, conditions)
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
            # FLO-177: Enrich startup sync embeds with trade conditions
            sync_conditions = _load_trade_conditions(ref["ticket"])
            sync_action = {
                "direction": ref.get("direction", ""),
                "entry_price": ref.get("entry_price"),
                "exit_price": ref.get("exit_price"),
                "pnl": ref.get("pnl"),
                "close_reason": ref.get("close_reason", ""),
            }
            text = _build_rich_embed_text(
                ref.get("lesson", ""),
                ref.get("pattern_tags", []),
                ref.get("thesis_summary", ""),
                sync_action,
                sync_conditions,
            )
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
        import time as _time

        # NOTE: Do NOT call mt5.shutdown() — it kills the process-global connection.
        # mt5.initialize() is safe to call if already initialized (returns True).
        if not mt5.initialize():
            return None

        # FLO-198: DB close_time is in MT5 server time (UTC+N), not UTC.
        # Compute server offset dynamically and convert to UTC for copy_rates_range.
        close_dt = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
        if close_dt.tzinfo is None:
            close_dt = close_dt.replace(tzinfo=timezone.utc)

        try:
            tick = mt5.symbol_info_tick("XAUUSD")
            if tick and tick.time:
                server_offset_s = int(tick.time) - int(_time.time())
                close_dt = close_dt - timedelta(seconds=server_offset_s)
        except Exception:
            pass

        target_1h = close_dt + timedelta(hours=1)
        bars = mt5.copy_rates_range("XAUUSD", mt5.TIMEFRAME_M5, close_dt, target_1h)

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

        # FLO-177: Update ChromaDB with enriched data
        try:
            hindsight_conditions = _load_trade_conditions(ticket)
            enriched_text = _build_rich_embed_text(
                revised_lesson,
                parsed.get("hindsight_tags", []),
                original.get("thesis_summary", ""),
                action,
                hindsight_conditions,
            )
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


# -------------------------------------------------------------------------
# FLO-269: Post-trade report generator (Steps 3 & 4)
# -------------------------------------------------------------------------

_REPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "post_trade_reports")


def _session_from_db_time(ts: str) -> str:
    """Classify session from a DB timestamp (broker time, not UTC).

    Uses config.MT5_SERVER_UTC_OFFSET to convert to real UTC hour.
    Same logic as sage_auditor.py (FLO-269 corrected).
    """
    try:
        import config as _cfg
        offset = int(getattr(_cfg, "MT5_SERVER_UTC_OFFSET", 2) or 2)
        dt = datetime.fromisoformat((ts or "").replace("Z", "+00:00"))
        broker_hour = dt.hour
        utc_hour = (broker_hour - offset) % 24
        if 0 <= utc_hour < 7:
            return "Asian"
        if 7 <= utc_hour < 13:
            return "London"
        if 13 <= utc_hour < 22:
            return "NY"
        return "OffHours"
    except Exception:
        return "unknown"


def generate_post_trade_report(action: Dict) -> Optional[Dict]:
    """Build a hard-data post-trade report from trades + trade_adjustments tables.

    Called immediately after a confirmed trade close. Counterfactual field
    is left as None — populated later by run_eod_counterfactuals() at 21:00 UTC.

    Args:
        action: closed trade dict from monitor (ticket, direction, open_price,
                close_price, profit, close_time, close_type, etc.)

    Returns:
        The report dict, or None on failure.
    """
    try:
        ticket = action.get("ticket")
        if not ticket:
            return None

        from db_writer import get_trade_adjustments

        import sqlite3
        import config as _cfg

        db_path = os.path.abspath(getattr(_cfg, "HISTORY_DB_PATH", "data/history.db"))
        conn = sqlite3.connect(db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM trades WHERE ticket = ?", (int(ticket),)
        ).fetchone()
        conn.close()

        if not row:
            log.debug(f"POST_TRADE_REPORT | ticket #{ticket} not in trades table")
            return None

        trade = dict(row)
        adjustments = get_trade_adjustments(int(ticket))

        # Original SL: first adjustment's old_sl, else trades.sl
        original_sl = trade.get("sl")
        if adjustments:
            original_sl = adjustments[0].get("old_sl") or original_sl

        # MFE / MAE / capture rate
        mfe = trade.get("mfe_points")
        mae = trade.get("mae_points")
        profit = action.get("profit") or trade.get("profit")
        final_sl = trade.get("final_sl") or action.get("orig_sl")

        capture_rate = None
        if mfe is not None and mfe > 0 and profit is not None:
            try:
                capture_rate = round((float(profit) / float(mfe)) * 100, 1)
            except Exception:
                pass

        # SL adjustment timeline
        sl_timeline = []
        for adj in adjustments:
            ts = adj.get("timestamp", "")
            # Compute minutes after open
            minutes_after = None
            try:
                open_time_str = trade.get("open_time") or action.get("open_time")
                if open_time_str and ts:
                    open_dt = datetime.fromisoformat(open_time_str.replace("Z", "+00:00"))
                    adj_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    if open_dt.tzinfo is None:
                        open_dt = open_dt.replace(tzinfo=timezone.utc)
                    if adj_dt.tzinfo is None:
                        adj_dt = adj_dt.replace(tzinfo=timezone.utc)
                    minutes_after = round((adj_dt - open_dt).total_seconds() / 60)
            except Exception:
                pass
            sl_timeline.append({
                "timestamp": ts,
                "minutes_after_open": minutes_after,
                "old_sl": adj.get("old_sl"),
                "new_sl": adj.get("new_sl"),
                "old_tp": adj.get("old_tp"),
                "new_tp": adj.get("new_tp"),
                "source": adj.get("source"),
            })

        # Duration
        duration_minutes = None
        try:
            open_t = trade.get("open_time") or action.get("open_time")
            close_t = trade.get("close_time") or action.get("close_time")
            if open_t and close_t:
                od = datetime.fromisoformat(open_t.replace("Z", "+00:00"))
                cd = datetime.fromisoformat(close_t.replace("Z", "+00:00"))
                if od.tzinfo is None:
                    od = od.replace(tzinfo=timezone.utc)
                if cd.tzinfo is None:
                    cd = cd.replace(tzinfo=timezone.utc)
                duration_minutes = round((cd - od).total_seconds() / 60)
        except Exception:
            pass

        # Session at open/close
        open_time_str = trade.get("open_time") or action.get("open_time") or ""
        close_time_str = trade.get("close_time") or action.get("close_time") or ""
        session_open = _session_from_db_time(open_time_str)
        session_close = _session_from_db_time(close_time_str)

        report = {
            "ticket": int(ticket),
            "generated_at": datetime.utcnow().isoformat(),
            "direction": action.get("direction") or trade.get("direction"),
            "entry_price": trade.get("open_price"),
            "close_price": action.get("close_price") or trade.get("close_price"),
            "original_sl": original_sl,
            "final_sl": final_sl,
            "tp": trade.get("tp"),
            "pnl": profit,
            "close_type": action.get("close_type"),
            "close_reason": action.get("reason") or trade.get("close_reason"),
            "open_time": trade.get("open_time"),
            "close_time": trade.get("close_time") or action.get("close_time"),
            "session_open": session_open,
            "session_close": session_close,
            "duration_minutes": duration_minutes,
            "mfe_points": mfe,
            "mae_points": mae,
            "capture_rate_pct": capture_rate,
            "sl_adjustments": sl_timeline,
            "sl_adjustment_count": len(sl_timeline),
            "counterfactual": None,  # Populated by run_eod_counterfactuals()
        }

        # Write to data/post_trade_reports/{ticket}.json
        os.makedirs(_REPORTS_DIR, exist_ok=True)
        report_path = os.path.join(_REPORTS_DIR, f"{ticket}.json")
        tmp = report_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, default=str)
        os.replace(tmp, report_path)

        _fmt_pnl = f"${float(profit):+.2f}" if profit is not None else "?"
        _fmt_cap = f"{capture_rate}%" if capture_rate is not None else "?"
        log.info(
            f"POST_TRADE_REPORT | #{ticket} | P&L={_fmt_pnl} | "
            f"MFE={mfe} | capture={_fmt_cap} | SL_adj={len(sl_timeline)}"
        )
        return report

    except Exception as e:
        log.warning(f"POST_TRADE_REPORT | failed for ticket={action.get('ticket')}: {e}")
        return None


def run_eod_counterfactuals() -> int:
    """End-of-day counterfactual replay for all trades closed today.

    Called at 21:00 UTC alongside Sage. For each trade:
    - Fetches M5 candles from close_time to now
    - Replays original SL and TP against candle highs/lows
    - Updates the report JSON with counterfactual results

    Returns:
        Number of reports enriched.
    """
    enriched = 0
    try:
        import sqlite3
        import config as _cfg
        import MetaTrader5 as mt5
        from datetime import timedelta
        import time as _time

        if not mt5.initialize():
            log.warning("EOD_COUNTERFACTUAL | MT5 not available")
            return 0

        # Get today's closed trades
        db_path = os.path.abspath(getattr(_cfg, "HISTORY_DB_PATH", "data/history.db"))
        conn = sqlite3.connect(db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        today = datetime.utcnow().strftime("%Y-%m-%d")
        rows = conn.execute(
            "SELECT ticket, direction, open_price, sl, tp, close_price, close_time "
            "FROM trades WHERE close_time LIKE ? AND close_price IS NOT NULL",
            (f"{today}%",),
        ).fetchall()
        conn.close()

        if not rows:
            log.info("EOD_COUNTERFACTUAL | no closed trades today")
            return 0

        from db_writer import get_trade_adjustments

        for row in rows:
            try:
                trade = dict(row)
                ticket = trade["ticket"]
                direction = (trade.get("direction") or "").upper()
                entry = trade.get("open_price")
                tp = trade.get("tp")
                close_time_str = trade.get("close_time")

                if not all([direction, entry, close_time_str]):
                    continue

                # Original SL: first adjustment's old_sl, else trades.sl
                adjustments = get_trade_adjustments(int(ticket))
                original_sl = trade.get("sl")
                if adjustments:
                    original_sl = adjustments[0].get("old_sl") or original_sl

                if original_sl is None:
                    continue

                # Fetch M5 candles from close_time to now
                close_dt = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
                if close_dt.tzinfo is None:
                    close_dt = close_dt.replace(tzinfo=timezone.utc)

                # Adjust for server time offset
                try:
                    tick = mt5.symbol_info_tick("XAUUSD")
                    if tick and tick.time:
                        server_offset_s = int(tick.time) - int(_time.time())
                        close_dt = close_dt - timedelta(seconds=server_offset_s)
                except Exception:
                    pass

                now_dt = datetime.utcnow()
                bars = mt5.copy_rates_range("XAUUSD", mt5.TIMEFRAME_M5, close_dt, now_dt)

                if bars is None or len(bars) == 0:
                    continue

                # Replay: walk candles, check if original SL or TP is hit
                original_sl_hit = False
                original_sl_hit_time = None
                tp_hit = False
                tp_hit_time = None
                tp_hit_pnl = None

                for bar in bars:
                    bar_time = datetime.utcfromtimestamp(int(bar[0])).isoformat()
                    bar_high = float(bar[2])
                    bar_low = float(bar[3])

                    if direction == "BUY":
                        # SL hit if low <= original_sl
                        if not original_sl_hit and bar_low <= float(original_sl):
                            original_sl_hit = True
                            original_sl_hit_time = bar_time
                        # TP hit if high >= tp
                        if tp and not tp_hit and bar_high >= float(tp):
                            tp_hit = True
                            tp_hit_time = bar_time
                            tp_hit_pnl = round(float(tp) - float(entry), 2)
                    else:  # SELL
                        if not original_sl_hit and bar_high >= float(original_sl):
                            original_sl_hit = True
                            original_sl_hit_time = bar_time
                        if tp and not tp_hit and bar_low <= float(tp):
                            tp_hit = True
                            tp_hit_time = bar_time
                            tp_hit_pnl = round(float(entry) - float(tp), 2)

                    # If both hit in same candle, SL takes priority (conservative)
                    if original_sl_hit and tp_hit and original_sl_hit_time == tp_hit_time:
                        tp_hit = False
                        tp_hit_time = None
                        tp_hit_pnl = None

                    # If SL hit before TP, TP doesn't count
                    if original_sl_hit and not tp_hit:
                        pass  # keep searching for TP only if SL not yet hit
                    if original_sl_hit and original_sl_hit_time and tp_hit_time:
                        if original_sl_hit_time < tp_hit_time:
                            # SL hit first — TP is moot
                            tp_hit = False
                            tp_hit_time = None
                            tp_hit_pnl = None

                # Hours of data available
                hours_of_data = len(bars) * 5 / 60

                counterfactual = {
                    "original_sl": float(original_sl),
                    "original_sl_survived": not original_sl_hit,
                    "original_sl_hit_time": original_sl_hit_time,
                    "tp_would_have_been_hit": tp_hit,
                    "tp_hit_time": tp_hit_time,
                    "tp_hit_pnl": tp_hit_pnl,
                    "hours_of_data": round(hours_of_data, 1),
                    "bars_analyzed": len(bars),
                    "analyzed_at": datetime.utcnow().isoformat(),
                }

                # Load existing report and update
                report_path = os.path.join(_REPORTS_DIR, f"{ticket}.json")
                if os.path.exists(report_path):
                    with open(report_path, "r", encoding="utf-8") as f:
                        report = json.load(f)
                    report["counterfactual"] = counterfactual
                    tmp = report_path + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(report, f, indent=2, default=str)
                    os.replace(tmp, report_path)
                else:
                    # No report from Step 3 — create minimal one
                    os.makedirs(_REPORTS_DIR, exist_ok=True)
                    report = {"ticket": ticket, "counterfactual": counterfactual}
                    tmp = report_path + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(report, f, indent=2, default=str)
                    os.replace(tmp, report_path)

                sl_msg = "survived" if not original_sl_hit else f"hit at {original_sl_hit_time}"
                tp_msg = f"hit at {tp_hit_time} = +${tp_hit_pnl}" if tp_hit else "not reached"
                log.info(
                    f"EOD_COUNTERFACTUAL | #{ticket} | orig_SL {sl_msg} | TP {tp_msg} | "
                    f"{len(bars)} bars ({hours_of_data:.1f}h)"
                )

                # FLO-269: Replace generic reflexion with rich data in ChromaDB
                try:
                    enrich_reflexion_with_report(ticket)
                except Exception:
                    pass

                enriched += 1

            except Exception as e:
                log.debug(f"EOD_COUNTERFACTUAL | error on ticket={row['ticket']}: {e}")

    except Exception as e:
        log.warning(f"EOD_COUNTERFACTUAL | failed: {e}")

    log.info(f"EOD_COUNTERFACTUAL | enriched {enriched} reports")
    return enriched


def enrich_reflexion_with_report(ticket: int) -> bool:
    """Replace generic reflexion lesson with data-rich text from post-trade report.

    Called after run_eod_counterfactuals() enriches the report with counterfactual data.
    Builds a rich text string and re-embeds in ChromaDB so semantic search
    returns real trade data instead of vague advice.

    Returns True if successfully re-embedded.
    """
    try:
        report_path = os.path.join(_REPORTS_DIR, f"{ticket}.json")
        if not os.path.exists(report_path):
            return False

        with open(report_path, "r", encoding="utf-8") as f:
            report = json.load(f)

        direction = report.get("direction", "?")
        entry = report.get("entry_price")
        pnl = report.get("pnl")
        mfe = report.get("mfe_points")
        mae = report.get("mae_points")
        capture = report.get("capture_rate_pct")
        sess_open = report.get("session_open", "?")
        sess_close = report.get("session_close", "?")
        adj_count = report.get("sl_adjustment_count", 0)
        orig_sl = report.get("original_sl")
        final_sl = report.get("final_sl")
        tp = report.get("tp")
        close_type = report.get("close_type", "?")
        duration = report.get("duration_minutes")

        # Build rich text
        parts = []
        parts.append(f"{direction} {entry} in {sess_open} session")
        if pnl is not None:
            parts.append(f"P&L ${float(pnl):+.2f} ({close_type})")
        if mfe is not None:
            parts.append(f"MFE +{mfe:.1f}pts")
        if mae is not None:
            parts.append(f"MAE {mae:.1f}pts")
        if capture is not None:
            parts.append(f"Capture {capture}%")
        if duration is not None:
            parts.append(f"Duration {duration}min")

        # SL adjustment summary
        if adj_count > 0:
            adjustments = report.get("sl_adjustments", [])
            adj_parts = []
            for a in adjustments:
                mins = a.get("minutes_after_open")
                src = a.get("source", "?")
                adj_parts.append(f"{a.get('old_sl')}->{a.get('new_sl')} at {mins}min ({src})")
            parts.append(f"{adj_count} SL adjustments: {'; '.join(adj_parts)}")
        else:
            parts.append("No SL adjustments")

        # Counterfactual
        cf = report.get("counterfactual")
        if cf:
            if cf.get("original_sl_survived") is False:
                parts.append(f"Original SL {orig_sl} would have been hit")
            elif cf.get("original_sl_survived") is True:
                parts.append(f"Original SL {orig_sl} survived")
            if cf.get("tp_would_have_been_hit"):
                parts.append(f"TP {tp} would have been hit = +${cf.get('tp_hit_pnl')}")
            else:
                parts.append(f"TP {tp} not reached in {cf.get('hours_of_data', 0):.0f}h")

            # Verdict
            if cf.get("original_sl_survived") is False and pnl is not None and orig_sl and entry:
                loss_avoided = abs(float(entry) - float(orig_sl))
                saved = round(float(pnl) + loss_avoided, 2)
                parts.append(f"SL adjustment SAVED ${saved:.2f}")
            elif cf.get("tp_would_have_been_hit") and cf.get("tp_hit_pnl") is not None and pnl is not None:
                cost = round(float(cf["tp_hit_pnl"]) - float(pnl), 2)
                parts.append(f"SL adjustment COST ${cost:.2f}")

        # Load trade conditions for regime/indicators
        conditions = _load_trade_conditions(ticket)
        if conditions:
            regime = conditions.get("regime")
            adx = conditions.get("adx_h1")
            rsi = conditions.get("rsi_h1")
            if regime:
                parts.append(f"Regime: {regime}")
            if adx is not None:
                parts.append(f"ADX {adx}")
            if rsi is not None:
                parts.append(f"RSI {rsi}")

        rich_text = " | ".join(parts)

        # Update lesson in trade_reflexions table
        try:
            from db_writer import _get_connection
            conn = _get_connection()
            try:
                conn.execute(
                    "UPDATE trade_reflexions SET lesson = ? WHERE ticket = ?",
                    (rich_text, int(ticket)),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            pass

        # Re-embed in ChromaDB
        success = _embed_reflexion(ticket, rich_text, {
            "ticket": int(ticket),
            "direction": direction,
            "pnl": float(pnl) if pnl is not None else 0,
            "lesson": rich_text[:500],
            "session": sess_open,
            "mfe": float(mfe) if mfe is not None else 0,
            "capture_pct": float(capture) if capture is not None else 0,
        })

        if success:
            log.info(f"RICH_REFLEXION | #{ticket} | re-embedded with report data ({len(rich_text)} chars)")
        return success

    except Exception as e:
        log.debug(f"RICH_REFLEXION | #{ticket} failed: {e}")
        return False


def get_last_trade_report_summary() -> Optional[str]:
    """Return a 2-3 line XML summary of the most recent post-trade report.

    Used by trigger_context injection (Step 5). Returns None if no report
    exists or report is stale (>24h).
    """
    try:
        if not os.path.isdir(_REPORTS_DIR):
            return None

        # Find most recent report by file mtime
        files = [
            os.path.join(_REPORTS_DIR, f)
            for f in os.listdir(_REPORTS_DIR)
            if f.endswith(".json")
        ]
        if not files:
            return None

        latest = max(files, key=os.path.getmtime)

        # Stale check: >24h old
        age_hours = (time.time() - os.path.getmtime(latest)) / 3600
        if age_hours > 24:
            return None

        with open(latest, "r", encoding="utf-8") as f:
            report = json.load(f)

        ticket = report.get("ticket", "?")
        direction = report.get("direction", "?")
        pnl = report.get("pnl")
        close_type = report.get("close_type", "?")
        mfe = report.get("mfe_points")
        mae = report.get("mae_points")
        capture = report.get("capture_rate_pct")
        adj_count = report.get("sl_adjustment_count", 0)

        # Build SL adjustment summary
        sl_summary = "none"
        adjustments = report.get("sl_adjustments", [])
        if adjustments:
            parts = []
            for a in adjustments:
                mins = a.get("minutes_after_open")
                src = a.get("source", "?")
                old = a.get("old_sl")
                new = a.get("new_sl")
                time_str = f"{mins}min" if mins is not None else "?"
                parts.append(f"{time_str}: {old}->{new} ({src})")
            sl_summary = "; ".join(parts)

        pnl_str = f"${float(pnl):+.2f}" if pnl is not None else "?"
        mfe_str = f"{mfe:+.1f}pts" if mfe is not None else "?"
        mae_str = f"{mae:+.1f}pts" if mae is not None else "?"
        cap_str = f"{capture}%" if capture is not None else "?"

        sess_open = report.get("session_open", "?")
        sess_close = report.get("session_close", "?")

        lines = [
            f'<last_trade_report ticket="{ticket}" direction="{direction}" pnl="{pnl_str}" close="{close_type}" session_open="{sess_open}" session_close="{sess_close}">',
            f"  MFE: {mfe_str} | MAE: {mae_str} | Captured: {cap_str} | SL changes: {adj_count} ({sl_summary})",
        ]

        # Counterfactual line (only if populated)
        cf = report.get("counterfactual")
        if cf:
            sl_survived = cf.get("original_sl_survived")
            tp_hit = cf.get("tp_would_have_been_hit")
            tp_time = cf.get("tp_hit_time", "")
            tp_pnl = cf.get("tp_hit_pnl")
            hours = cf.get("hours_of_data", 0)

            cf_parts = []
            if sl_survived is True:
                cf_parts.append("original SL survived")
            elif sl_survived is False:
                cf_parts.append(f"original SL hit at {cf.get('original_sl_hit_time', '?')}")
            if tp_hit:
                cf_parts.append(f"TP hit at {tp_time} = +${tp_pnl}")
            elif sl_survived:
                cf_parts.append(f"TP not reached in {hours:.0f}h window")
            lines.append(f"  Counterfactual ({hours:.0f}h window): {', '.join(cf_parts)}")

        lines.append("</last_trade_report>")
        return "\n".join(lines)

    except Exception as e:
        log.debug(f"POST_TRADE_REPORT | summary failed: {e}")
        return None
