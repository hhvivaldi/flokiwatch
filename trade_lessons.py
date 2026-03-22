"""
Trade Lessons — Dynamic lessons from Floki's past trades (FLO-63)

Step 1: save_trade_conditions() — capture indicator snapshot at trade OPEN time
Step 2: extract_trade_lesson() — after trade CLOSE, categorize into pattern bucket
        and update trade_lessons.json with win/loss statistics

All extraction is DETERMINISTIC Python — no AI calls.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from logger import log

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
CONDITIONS_DIR = os.path.join(DATA_DIR, "trade_conditions")
LESSONS_FILE = os.path.join(DATA_DIR, "trade_lessons.json")


# ---------------------------------------------------------------------------
# RSI / Volume / Session bucket definitions
# ---------------------------------------------------------------------------

def _rsi_bucket(rsi: Optional[float]) -> str:
    if rsi is None:
        return "UNKNOWN"
    if rsi < 30:
        return "OVERSOLD"
    if rsi < 50:
        return "WEAK"
    if rsi < 70:
        return "NEUTRAL"
    return "OVERBOUGHT"


def _volume_bucket(volume: Optional[float]) -> str:
    if volume is None:
        return "UNKNOWN"
    if volume < 8000:
        return "LOW"
    if volume < 15000:
        return "NORMAL"
    return "HIGH"


def _session_from_hour(utc_hour: Optional[int]) -> str:
    if utc_hour is None:
        return "UNKNOWN"
    h = int(utc_hour) % 24
    if 0 <= h < 8:
        return "ASIAN"
    if 8 <= h < 14:
        return "LONDON"
    return "NY"


def _luna_env_bucket(env: Optional[str]) -> str:
    if not env:
        return "UNKNOWN"
    e = str(env).upper().strip()
    if e in ("SAFE", "CAUTION", "DANGER"):
        return e
    return "UNKNOWN"


# ---------------------------------------------------------------------------
# Step 1: Save conditions at trade OPEN
# ---------------------------------------------------------------------------

def save_trade_conditions(
    ticket: int,
    direction: str,
    conditions: Dict[str, Any],
) -> bool:
    """
    Save trade conditions snapshot at open time.
    Called from agent_tools.py execute_trade after successful execution.
    Returns True on success, False on failure. Never raises.
    """
    try:
        os.makedirs(CONDITIONS_DIR, exist_ok=True)

        snapshot = {
            "ticket": int(ticket),
            "direction": str(direction).upper(),
            "open_time": datetime.now(timezone.utc).isoformat(),
            "conditions_at_open": conditions,
        }

        path = os.path.join(CONDITIONS_DIR, f"{ticket}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, indent=2, default=str)

        log.info(f"LESSONS | conditions saved for ticket #{ticket}")
        return True
    except Exception as e:
        log.warning(f"LESSONS | failed to save conditions for #{ticket}: {e}")
        return False


# ---------------------------------------------------------------------------
# Step 2: Extract lesson after trade CLOSE
# ---------------------------------------------------------------------------

def _build_bucket_key(direction: str, conditions: Dict[str, Any]) -> str:
    """Build a deterministic pattern bucket key from conditions."""
    rsi = _rsi_bucket(conditions.get("rsi_h1"))
    vol = _volume_bucket(conditions.get("volume_h1"))
    session = conditions.get("session") or _session_from_hour(conditions.get("utc_hour"))
    luna = _luna_env_bucket(conditions.get("luna_environment"))
    d = str(direction).upper()

    return f"{d} | RSI {rsi} | Vol {vol} | {session} | {luna}"


def _load_lessons() -> List[Dict[str, Any]]:
    try:
        if os.path.exists(LESSONS_FILE):
            with open(LESSONS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []


def _save_lessons(lessons: List[Dict[str, Any]]) -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = LESSONS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(lessons, f, indent=2, ensure_ascii=False, default=str)
        os.replace(tmp, LESSONS_FILE)
    except Exception as e:
        log.warning(f"LESSONS | failed to save lessons file: {e}")


def _generate_lesson_text(bucket_key: str, wins: int, losses: int, avg_pnl: float) -> str:
    """Deterministic lesson text based on statistics."""
    total = wins + losses
    if total < 3:
        return f"NEUTRAL: {bucket_key} — insufficient data ({total} trades)"

    win_rate = (wins / total) * 100 if total > 0 else 0

    if win_rate < 30:
        return f"AVOID: {bucket_key} — {wins}/{total} wins ({win_rate:.0f}%), avg P&L ${avg_pnl:+.2f}"
    if win_rate > 70:
        return f"PREFERRED: {bucket_key} — {wins}/{total} wins ({win_rate:.0f}%), avg P&L ${avg_pnl:+.2f}"
    return f"NEUTRAL: {bucket_key} — {wins}/{total} wins ({win_rate:.0f}%), avg P&L ${avg_pnl:+.2f}"


def extract_trade_lesson(ticket: int, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    After a trade closes, extract the lesson by combining:
    - Conditions snapshot from data/trade_conditions/{ticket}.json
    - Outcome from history.db

    Updates data/trade_lessons.json with the new data point.
    Returns the updated lesson dict, or None on failure. Never raises.
    """
    try:
        # 1. Load condition snapshot
        cond_path = os.path.join(CONDITIONS_DIR, f"{ticket}.json")
        if not os.path.exists(cond_path):
            return None  # No conditions saved (trade opened before FLO-63)

        with open(cond_path, "r", encoding="utf-8") as f:
            snapshot = json.load(f)

        conditions = snapshot.get("conditions_at_open", {})
        direction = snapshot.get("direction", "UNKNOWN")

        # 2. Get outcome from history.db
        path = db_path or os.path.join(DATA_DIR, "history.db")
        if not os.path.exists(path):
            return None

        conn = sqlite3.connect(path, timeout=5)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT profit, open_time, close_time FROM trades WHERE ticket = ?",
            (int(ticket),),
        ).fetchone()
        conn.close()

        if not row or row["profit"] is None:
            return None

        profit = float(row["profit"])
        is_win = profit > 0

        # 3. Build bucket key
        bucket_key = _build_bucket_key(direction, conditions)

        # 4. Update lessons
        lessons = _load_lessons()

        # Find existing lesson for this bucket
        existing = None
        for lesson in lessons:
            if lesson.get("bucket") == bucket_key:
                existing = lesson
                break

        if existing:
            existing["occurrences"] = existing.get("occurrences", 0) + 1
            if is_win:
                existing["wins"] = existing.get("wins", 0) + 1
            else:
                existing["losses"] = existing.get("losses", 0) + 1

            # Recalculate avg_pnl as running average
            prev_total = existing["occurrences"] - 1
            prev_avg = existing.get("avg_pnl", 0)
            existing["avg_pnl"] = round(
                (prev_avg * prev_total + profit) / existing["occurrences"], 2
            )
            existing["last_occurrence"] = datetime.now(timezone.utc).isoformat()
            existing["lesson"] = _generate_lesson_text(
                bucket_key, existing["wins"], existing["losses"], existing["avg_pnl"]
            )
            updated = existing
        else:
            new_lesson = {
                "bucket": bucket_key,
                "occurrences": 1,
                "wins": 1 if is_win else 0,
                "losses": 0 if is_win else 1,
                "avg_pnl": round(profit, 2),
                "last_occurrence": datetime.now(timezone.utc).isoformat(),
                "lesson": _generate_lesson_text(
                    bucket_key, 1 if is_win else 0, 0 if is_win else 1, profit
                ),
            }
            lessons.append(new_lesson)
            updated = new_lesson

        _save_lessons(lessons)

        win_rate = (updated["wins"] / updated["occurrences"] * 100) if updated["occurrences"] > 0 else 0
        log.info(
            f"LESSONS | #{ticket} → {bucket_key} | "
            f"{'WIN' if is_win else 'LOSS'} ${profit:+.2f} | "
            f"bucket: {updated['occurrences']} trades, {win_rate:.0f}% WR"
        )

        return updated

    except Exception as e:
        log.warning(f"LESSONS | failed to extract lesson for #{ticket}: {e}")
        return None


# ---------------------------------------------------------------------------
# Query lessons (for get_trade_lessons tool)
# ---------------------------------------------------------------------------

def get_relevant_lessons(min_occurrences: int = 3, limit: int = 10) -> List[Dict[str, Any]]:
    """
    Return top lessons sorted by relevance:
    - AVOID lessons first (< 30% win rate)
    - PREFERRED lessons second (> 70% win rate)
    - Only lessons with min_occurrences+ trades
    """
    lessons = _load_lessons()

    # Filter by minimum occurrences
    meaningful = [l for l in lessons if l.get("occurrences", 0) >= min_occurrences]

    # Sort: AVOID first, then PREFERRED, then NEUTRAL
    def sort_key(l):
        text = l.get("lesson", "")
        if text.startswith("AVOID"):
            return (0, -l.get("occurrences", 0))
        if text.startswith("PREFERRED"):
            return (1, -l.get("occurrences", 0))
        return (2, -l.get("occurrences", 0))

    meaningful.sort(key=sort_key)
    return meaningful[:limit]
