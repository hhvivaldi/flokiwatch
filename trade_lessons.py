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
import subprocess
from datetime import datetime, timedelta, timezone
from tz_utils import utc_iso  # FLO-309
from typing import Any, Dict, List, Optional

from logger import log

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
CONDITIONS_DIR = os.path.join(DATA_DIR, "trade_conditions")
LESSONS_FILE = os.path.join(DATA_DIR, "trade_lessons.json")

# FLO-328: git SHA for system_version tagging. Cached after first lookup.
_CURRENT_SHA_CACHE: Optional[str] = None


def _current_sha() -> str:
    """Return current git HEAD short SHA. Cached. 'unknown' on failure."""
    global _CURRENT_SHA_CACHE
    if _CURRENT_SHA_CACHE is not None:
        return _CURRENT_SHA_CACHE
    try:
        repo_dir = os.path.dirname(os.path.abspath(__file__))
        result = subprocess.run(
            ["git", "rev-parse", "--short=7", "HEAD"],
            capture_output=True, text=True, timeout=5, cwd=repo_dir,
        )
        if result.returncode == 0 and result.stdout.strip():
            _CURRENT_SHA_CACHE = result.stdout.strip()
            return _CURRENT_SHA_CACHE
    except Exception:
        pass
    _CURRENT_SHA_CACHE = "unknown"
    return _CURRENT_SHA_CACHE


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


# FLO-336: _luna_env_bucket removed. Bug G killed the write path (execute_trade
# no longer captures luna_environment); _build_bucket_key no longer reads it.


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
            "open_time": utc_iso(),  # FLO-309: Z suffix per Rule 22
            "system_version": _current_sha(),  # FLO-328: era tag for lessons filter
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
    """Build a deterministic pattern bucket key from conditions.

    FLO-336: removed luna_environment dimension. Bug G (7b5f8a9) killed the
    write path in execute_trade; the read path here now drops the field too
    so historical CAUTION/DANGER values stop surfacing through lessons.
    """
    rsi = _rsi_bucket(conditions.get("rsi_h1"))
    vol = _volume_bucket(conditions.get("volume_h1"))
    session = conditions.get("session") or _session_from_hour(conditions.get("utc_hour"))
    d = str(direction).upper()

    return f"{d} | RSI {rsi} | Vol {vol} | {session}"


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
    """Factual lesson text — statistics only, no directive prefixes.

    FLO-336: removed AVOID:/PREFERRED:/NEUTRAL: prefixes. Prior framing
    (FLO-63, commit 21f83f2) prescribed action; Escola 1 v2.0 supersedes.
    Floki reads the numbers and decides for himself.
    """
    total = wins + losses
    if total < 3:
        return f"{bucket_key} — insufficient data ({total} trades)"

    win_rate = (wins / total) * 100 if total > 0 else 0
    return f"{bucket_key} — {wins}/{total} wins ({win_rate:.0f}% WR), avg P&L ${avg_pnl:+.2f}"


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

        # FLO-327: dedup guard. Without this, extract_trade_lesson incremented
        # on every call — and the close-handler path is re-entered by
        # reconciliation loops, inflating the real count by 2-3×. Confirmed
        # via log forensics: ticket #1581152281 hit the counter 3 times, and
        # the AVOID classification on `SELL | RSI WEAK | NY | CAUTION` only
        # existed because 2 real trades were counted as 6.
        _ticket_int = int(ticket)
        if existing:
            seen = existing.setdefault("processed_tickets", [])
            if _ticket_int in seen:
                log.debug(f"LESSONS | #{_ticket_int} → {bucket_key} | already processed, skipping (FLO-327 dedup)")
                return existing
            seen.append(_ticket_int)

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
            existing["last_occurrence"] = utc_iso()  # FLO-309
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
                "processed_tickets": [_ticket_int],  # FLO-327
                "last_occurrence": utc_iso(),  # FLO-309
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

def get_relevant_lessons(
    min_occurrences: int = 3,
    limit: int = 10,
    window_days: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """FLO-328: Compute lessons fresh from trade_conditions/ + history.db.

    Previously read from trade_lessons.json (persistent aggregated state),
    which had two problems: duplicate-counting corruption (FLO-327) and
    no mechanism to expire stale system-era data. This implementation:

      - aggregates in-memory on every call (trivial: ~50ms for 100 files)
      - applies TWO filters per trade before it contributes to a bucket:
          age  ≤ config.LESSONS_WINDOW_DAYS (default 30)
          era  snapshot.system_version ∈ config.LESSONS_CURRENT_ERA_SHAS
        Both filters must pass.
      - sorts AVOID > PREFERRED > NEUTRAL, returns top `limit`
      - returns [] when no trades qualify yet (era boundary or cold start)

    trade_lessons.json still gets written by extract_trade_lesson() but is
    now an audit log — no longer the source of truth for this function.
    """
    try:
        import config as _cfg
    except Exception:
        _cfg = None
    era_list = list(getattr(_cfg, "LESSONS_CURRENT_ERA_SHAS", []))
    if not era_list:
        return []

    win_days = int(window_days if window_days is not None
                   else getattr(_cfg, "LESSONS_WINDOW_DAYS", 30) or 30)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=win_days)

    if not os.path.exists(CONDITIONS_DIR):
        return []

    db_path = os.path.join(DATA_DIR, "history.db")
    if not os.path.exists(db_path):
        return []

    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        # Pre-fetch all profits in one query to avoid N db hits
        profits: Dict[int, Optional[float]] = {}
        try:
            for row in conn.execute(
                "SELECT ticket, profit FROM trades WHERE profit IS NOT NULL"
            ).fetchall():
                profits[int(row["ticket"])] = float(row["profit"])
        except Exception:
            pass

        buckets: Dict[str, Dict[str, Any]] = {}
        processed = skipped_era = skipped_age = skipped_noprofit = 0

        for fn in os.listdir(CONDITIONS_DIR):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(CONDITIONS_DIR, fn), "r", encoding="utf-8") as f:
                    snap = json.load(f)
            except Exception:
                continue

            # Era filter
            sv = snap.get("system_version")
            if sv not in era_list:
                skipped_era += 1
                continue

            # Age filter — parse open_time (stored as UTC ISO per FLO-309)
            try:
                ot_str = str(snap.get("open_time") or "").replace("Z", "+00:00")
                ot_dt = datetime.fromisoformat(ot_str)
                if ot_dt.tzinfo is None:
                    ot_dt = ot_dt.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            if ot_dt < cutoff:
                skipped_age += 1
                continue

            ticket = snap.get("ticket")
            if ticket is None or int(ticket) == 0:
                continue
            profit = profits.get(int(ticket))
            if profit is None:
                skipped_noprofit += 1
                continue

            is_win = profit > 0
            direction = snap.get("direction", "UNKNOWN")
            conds = snap.get("conditions_at_open", {}) or {}
            key = _build_bucket_key(direction, conds)

            b = buckets.setdefault(key, {
                "bucket": key, "occurrences": 0,
                "wins": 0, "losses": 0, "pnl_sum": 0.0,
                "last_occurrence": snap.get("open_time"),
            })
            b["occurrences"] += 1
            if is_win: b["wins"] += 1
            else:      b["losses"] += 1
            b["pnl_sum"] += profit
            cur_last = str(b["last_occurrence"] or "")
            if str(snap.get("open_time") or "") > cur_last:
                b["last_occurrence"] = snap.get("open_time")
            processed += 1
    finally:
        conn.close()

    log.debug(
        f"LESSONS_AGG | era={era_list} window={win_days}d | "
        f"processed={processed} skip_era={skipped_era} skip_age={skipped_age} skip_noprofit={skipped_noprofit}"
    )

    out: List[Dict[str, Any]] = []
    for key, b in buckets.items():
        if b["occurrences"] < min_occurrences:
            continue
        avg = round(b["pnl_sum"] / b["occurrences"], 2)
        out.append({
            "bucket": key,
            "occurrences": b["occurrences"],
            "wins": b["wins"],
            "losses": b["losses"],
            "avg_pnl": avg,
            "last_occurrence": b["last_occurrence"],
            "lesson": _generate_lesson_text(key, b["wins"], b["losses"], avg),
        })

    def _sort_key(l):
        # FLO-336: sort by win-rate extremes (lowest WR first = most cautionary
        # statistical signal), then higher WR, then mid, then by sample size.
        # Replaces prior AVOID/PREFERRED/NEUTRAL prefix parse (editorial label removed).
        occ = max(1, int(l.get("occurrences") or 0))
        wr = (int(l.get("wins") or 0) / occ) * 100
        if wr < 30:    return (0, -occ)
        if wr > 70:    return (1, -occ)
        return (2, -occ)

    out.sort(key=_sort_key)
    return out[:limit]
