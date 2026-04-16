"""
FLO-325: Floki's permanent process-memory layer.

Separate from three existing learning systems:
  - trade_lessons.json : pattern buckets × outcomes (auto-populated on
                        trade CLOSE). Answers "what's my WR in
                        RANGING/NY/DANGER?"
  - reflexions (Chroma): per-trade rich analysis with embeddings.
                        Answers "tell me about similar losing SELLs".
  - session_memory     : in-day thesis + notes, resets daily.
  - THIS FILE (new)    : lightweight process learnings Floki curates
                        himself. Survives restarts. Answers "what have
                        I learned about MY DECISION PROCESS?"

Philosophy: Floki-driven writes only. No auto-save from every
self_critique (noise explosion). Hard cap 50 entries with FIFO
auto-drop — newest wins when Floki rewrites a lesson he values, so
FIFO naturally favors currently-relevant learnings.

Schema (data/floki_lessons.json):
  {
    "next_id": 52,       // monotonic counter, survives FIFO drops
    "lessons": [
      {
        "id": 47,
        "timestamp": "2026-04-15T19:46:48Z",
        "lesson": "In ranging regime, don't enter at range extremes
                   without volume profile confirmation at the HVN.",
        "context": {
          "regime": "RANGING",         // optional
          "session": "NY",              // optional
          "related_ticket": 1593492605  // optional, if tied to a trade
        }
      }
    ]
  }

All functions safe on missing/malformed file → empty list, never raise.
"""
import json
import os
from datetime import timezone
from typing import Any, Dict, List, Optional

from logger import log
from tz_utils import utc_iso


_LESSONS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "floki_lessons.json"
)
_MAX_LESSONS = 50
_LESSON_MAX_CHARS = 400  # per-lesson text cap


def _load() -> Dict[str, Any]:
    try:
        if not os.path.exists(_LESSONS_PATH):
            return {"next_id": 1, "lessons": []}
        with open(_LESSONS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"next_id": 1, "lessons": []}
        data.setdefault("next_id", 1)
        if not isinstance(data.get("lessons"), list):
            data["lessons"] = []
        return data
    except Exception as e:
        log.debug(f"floki_lessons: load failed (ignored): {e}")
        return {"next_id": 1, "lessons": []}


def _save(data: Dict[str, Any]) -> bool:
    try:
        os.makedirs(os.path.dirname(_LESSONS_PATH), exist_ok=True)
        tmp = _LESSONS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, _LESSONS_PATH)
        return True
    except Exception as e:
        log.debug(f"floki_lessons: save failed (ignored): {e}")
        return False


def get_lessons() -> List[Dict[str, Any]]:
    """Return the current lessons list (newest last)."""
    return _load().get("lessons", [])


def save_lesson(text: str, context: Optional[Dict[str, Any]] = None) -> Optional[int]:
    """Append a lesson. Returns the assigned id. FIFO-drops the oldest
    when cap reached.

    If an existing lesson has matching text (trimmed, case-insensitive),
    it's moved to newest (bumped) with a fresh timestamp — this lets
    Floki "refresh" a valuable lesson to escape FIFO eviction.
    """
    clean = str(text or "").strip()[:_LESSON_MAX_CHARS]
    if not clean:
        return None
    try:
        data = _load()
        lessons = data.get("lessons", [])

        # Bump duplicate if present (case-insensitive text match)
        lower = clean.lower()
        bumped_id = None
        kept: List[Dict[str, Any]] = []
        for l in lessons:
            if isinstance(l, dict) and str(l.get("lesson") or "").strip().lower() == lower:
                bumped_id = l.get("id")
                continue  # drop old position; will re-append as newest
            kept.append(l)

        if bumped_id is not None:
            new_entry = {
                "id": bumped_id,
                "timestamp": utc_iso(),
                "lesson": clean,
                "context": _sanitize_context(context),
            }
            kept.append(new_entry)
            data["lessons"] = kept
            _save(data)
            log.info(f"FLOKI_LESSON | BUMP id={bumped_id} | '{clean[:80]}'")
            return bumped_id

        # New lesson
        next_id = int(data.get("next_id", 1))
        new_entry = {
            "id": next_id,
            "timestamp": utc_iso(),
            "lesson": clean,
            "context": _sanitize_context(context),
        }
        kept.append(new_entry)
        data["next_id"] = next_id + 1

        # FIFO cap — drop oldest until under limit
        dropped: List[int] = []
        while len(kept) > _MAX_LESSONS:
            d = kept.pop(0)
            if isinstance(d, dict):
                dropped.append(int(d.get("id") or -1))

        data["lessons"] = kept
        _save(data)
        if dropped:
            log.info(f"FLOKI_LESSON | ADD id={next_id} | FIFO dropped={dropped} | '{clean[:80]}'")
        else:
            log.info(f"FLOKI_LESSON | ADD id={next_id} | '{clean[:80]}'")
        return next_id
    except Exception as e:
        log.debug(f"floki_lessons: save_lesson failed (ignored): {e}")
        return None


def forget_lesson(lesson_id: int) -> bool:
    """Remove a lesson by id. Returns True if found + removed."""
    try:
        data = _load()
        lessons = data.get("lessons", [])
        before = len(lessons)
        kept = [l for l in lessons if not (isinstance(l, dict) and int(l.get("id", -1)) == int(lesson_id))]
        if len(kept) == before:
            return False
        data["lessons"] = kept
        _save(data)
        log.info(f"FLOKI_LESSON | FORGET id={lesson_id}")
        return True
    except Exception as e:
        log.debug(f"floki_lessons: forget_lesson failed (ignored): {e}")
        return False


def _sanitize_context(ctx: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Keep only allowed context keys, coerce to strings/ints."""
    if not isinstance(ctx, dict):
        return {}
    out: Dict[str, Any] = {}
    regime = ctx.get("regime")
    if isinstance(regime, str) and regime.strip():
        out["regime"] = regime.strip()[:40]
    session = ctx.get("session")
    if isinstance(session, str) and session.strip():
        out["session"] = session.strip()[:20]
    source = ctx.get("source")
    if isinstance(source, str) and source.strip():
        out["source"] = source.strip()[:40]
    ltype = ctx.get("type")
    if isinstance(ltype, str) and ltype.strip():
        out["type"] = ltype.strip()[:40]
    rt = ctx.get("related_ticket")
    if rt is not None:
        try:
            out["related_ticket"] = int(rt)
        except Exception:
            pass
    return out


def render_block() -> str:
    """Render active lessons as a <lessons_learned> XML block for prompt
    injection. Returns empty string when no lessons exist so callers can
    concat unconditionally.
    """
    lessons = get_lessons()
    if not lessons:
        return ""

    lines: List[str] = [
        "<lessons_learned>",
        "Your accumulated process learnings. Review them before planning tools and deciding. "
        "These survive restarts and day rollovers. Call `save_lesson(text, context?)` to add a new one "
        "or bump an existing match to newest. Call `forget_lesson(id)` to drop one. "
        f"FIFO cap is {_MAX_LESSONS} — oldest drops automatically on add.",
        "",
    ]
    # Newest last in the file, but show newest first for review (inverse)
    for l in reversed(lessons):
        if not isinstance(l, dict):
            continue
        lid = l.get("id", "?")
        ts = str(l.get("timestamp") or "")[:10]  # YYYY-MM-DD only
        ctx = l.get("context") or {}
        ctx_bits = []
        if ctx.get("source"): ctx_bits.append(f"from {ctx['source']}")
        if ctx.get("type"): ctx_bits.append(str(ctx["type"]).replace("_", " "))
        if ctx.get("regime"): ctx_bits.append(str(ctx["regime"]))
        if ctx.get("session"): ctx_bits.append(str(ctx["session"]))
        if ctx.get("related_ticket"): ctx_bits.append(f"#{ctx['related_ticket']}")
        ctx_str = f"  ({', '.join(ctx_bits)})" if ctx_bits else ""
        lines.append(f"[{lid}] {ts}{ctx_str}  {str(l.get('lesson') or '').strip()}")
    lines.append("</lessons_learned>")
    return "\n".join(lines)
