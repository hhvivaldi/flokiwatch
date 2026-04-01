import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import config
from logger import log


_PATTERNS_FILE_REL = os.path.join("data", "agent_patterns.json")


@dataclass(frozen=True)
class TradeFeatureRow:
    ticket: int
    direction: str
    profit: float
    open_time: str
    close_time: str
    session_name: Optional[str]
    utc_hour: Optional[int]
    rsi_14: Optional[float]
    mtf_alignment: Optional[str]
    mtf_d1_direction: Optional[str]
    mtf_h4_direction: Optional[str]
    volume_ratio: Optional[float]
    volume_classification: Optional[str]
    brain_confidence: Optional[float]


class ReflectionEngine:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = False

    def run_async(self, reason: str) -> None:
        try:
            with self._lock:
                if self._running:
                    log.info("REFLECTION | skipped | already_running")
                    return
                self._running = True

            t = threading.Thread(target=self._run_safe, args=(reason,), daemon=True)
            t.start()
        except Exception as e:
            try:
                log.debug(f"REFLECTION | failed_to_start | error={e}")
            except Exception:
                pass

    def _run_safe(self, reason: str) -> None:
        start = time.time()
        try:
            log.info(f"REFLECTION | start | reason={reason}")
            payload, top_edge, worst = build_patterns_payload()
            ok = write_patterns_atomic(payload)

            dt_ms = int((time.time() - start) * 1000)
            if ok:
                trade_count = int(payload.get("trade_count", 0) or 0)
                patterns = payload.get("patterns") if isinstance(payload.get("patterns"), list) else []
                msg = f"REFLECTION | done | trade_count={trade_count} | patterns={len(patterns)} | ms={dt_ms}"
                if top_edge:
                    msg += f" | top_edge=\"{top_edge}\""
                if worst:
                    msg += f" | worst=\"{worst}\""
                log.info(msg)
            else:
                log.warning(f"REFLECTION | failed_to_write | ms={dt_ms}")
        except Exception as e:
            try:
                log.debug(f"REFLECTION | error | {e}")
            except Exception:
                pass
        finally:
            try:
                with self._lock:
                    self._running = False
            except Exception:
                pass


_engine = ReflectionEngine()


def run_reflection_async(reason: str) -> None:
    _engine.run_async(reason=str(reason or ""))


def _get_connection() -> sqlite3.Connection:
    db_path = os.path.abspath(getattr(config, "HISTORY_DB_PATH", "data/history.db"))
    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _safe_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        return float(v)
    except Exception:
        return None


def _safe_int(v: Any) -> Optional[int]:
    try:
        if v is None:
            return None
        return int(v)
    except Exception:
        return None


def _infer_session_from_utc_hour(utc_hour: Optional[int]) -> Optional[str]:
    if utc_hour is None:
        return None
    try:
        h = int(utc_hour) % 24
    except Exception:
        return None

    if 0 <= h <= 6:
        return "ASIAN"
    if 7 <= h <= 12:
        return "LONDON"
    if 13 <= h <= 20:
        return "NY"
    return "OFF"


def _parse_utc_hour_from_iso(iso_ts: str) -> Optional[int]:
    try:
        if not iso_ts:
            return None
        s = str(iso_ts).replace("Z", "")
        dt = datetime.fromisoformat(s)
        return int(dt.hour)
    except Exception:
        return None


def _rsi_bucket(rsi: Optional[float]) -> Optional[str]:
    if rsi is None:
        return None
    try:
        v = float(rsi)
    except Exception:
        return None

    if v < 30:
        return "<30"
    if v < 40:
        return "30-40"
    if v <= 60:
        return "40-60"
    if v <= 70:
        return "60-70"
    return ">70"


def _volume_bucket(vr: Optional[float]) -> Optional[str]:
    if vr is None:
        return None
    try:
        v = float(vr)
    except Exception:
        return None

    if v < 0.1:
        return "<0.1"
    if v < 0.5:
        return "0.1-0.5"
    if v <= 1.2:
        return "0.5-1.2"
    return ">1.2"


def _confidence_bucket(conf: Optional[float]) -> Optional[str]:
    if conf is None:
        return None
    try:
        v = float(conf)
    except Exception:
        return None

    if v < 55:
        return "<55"
    if v < 65:
        return "55-64"
    if v < 75:
        return "65-74"
    return ">=75"


def _compute_pf(wins: List[float], losses: List[float]) -> float:
    w = sum([x for x in wins if x > 0])
    l = abs(sum([x for x in losses if x < 0]))
    if l <= 0:
        return round(w, 3) if w > 0 else 0.0
    return round(w / l, 3)


def _insight_label(trades: int, wr: float, pf: float) -> str:
    if trades < 5:
        return "Insufficient data"
    if wr >= 65.0 and pf >= 1.5:
        return "Strong edge"
    if wr <= 40.0 and pf <= 0.8:
        return "Avoid — losing pattern"
    return "Neutral"


def _query_closed_trades(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    # FLO-189: Only Floki trades — exclude legacy agent_gemini/brain
    cur = conn.execute(
        """
        SELECT ticket, direction, profit, open_time, close_time
        FROM trades
        WHERE close_time IS NOT NULL
          AND profit IS NOT NULL
          AND decision_source IN ('floki_agent', 'agent_floki')
        ORDER BY close_time ASC
        """
    )
    return list(cur.fetchall() or [])


def _find_analysis_features_for_trade(
    conn: sqlite3.Connection, open_time: str
) -> Optional[sqlite3.Row]:
    if not open_time:
        return None

    cur = conn.execute(
        """
        SELECT timestamp, utc_hour, session_name, rsi_14,
               mtf_alignment, mtf_d1_direction, mtf_h4_direction,
               volume_ratio, volume_classification,
               confidence
        FROM analyses
        WHERE timestamp <= ?
        ORDER BY timestamp DESC
        LIMIT 1
        """,
        (open_time,),
    )
    row = cur.fetchone()
    if not row:
        return None

    try:
        t_trade = datetime.fromisoformat(str(open_time).replace("Z", ""))
        t_ana = datetime.fromisoformat(str(row["timestamp"]).replace("Z", ""))
        gap = (t_trade - t_ana).total_seconds()
        if gap < 0 or gap > 5 * 60:
            return None
    except Exception:
        return None

    return row


def _build_feature_rows() -> Tuple[List[TradeFeatureRow], int]:
    conn = _get_connection()
    try:
        trades = _query_closed_trades(conn)
        out: List[TradeFeatureRow] = []

        for tr in trades:
            try:
                ticket = int(tr["ticket"] or 0)
                direction = str(tr["direction"] or "").upper().strip()
                profit = float(tr["profit"] or 0.0)
                open_time = str(tr["open_time"] or "")
                close_time = str(tr["close_time"] or "")

                a = _find_analysis_features_for_trade(conn, open_time=open_time)

                session_name = None
                utc_hour = None
                rsi_14 = None
                mtf_alignment = None
                mtf_d1_direction = None
                mtf_h4_direction = None
                volume_ratio = None
                volume_classification = None
                brain_confidence = None

                if a is not None:
                    session_name = str(a["session_name"] or "").strip() or None
                    utc_hour = _safe_int(a["utc_hour"])
                    rsi_14 = _safe_float(a["rsi_14"])
                    mtf_alignment = str(a["mtf_alignment"] or "").strip() or None
                    mtf_d1_direction = str(a["mtf_d1_direction"] or "").strip() or None
                    mtf_h4_direction = str(a["mtf_h4_direction"] or "").strip() or None
                    volume_ratio = _safe_float(a["volume_ratio"])
                    volume_classification = str(a["volume_classification"] or "").strip() or None
                    brain_confidence = _safe_float(a["confidence"])

                if utc_hour is None:
                    utc_hour = _parse_utc_hour_from_iso(open_time)

                if session_name is None:
                    session_name = _infer_session_from_utc_hour(utc_hour)

                out.append(
                    TradeFeatureRow(
                        ticket=ticket,
                        direction=direction,
                        profit=profit,
                        open_time=open_time,
                        close_time=close_time,
                        session_name=session_name,
                        utc_hour=utc_hour,
                        rsi_14=rsi_14,
                        mtf_alignment=mtf_alignment,
                        mtf_d1_direction=mtf_d1_direction,
                        mtf_h4_direction=mtf_h4_direction,
                        volume_ratio=volume_ratio,
                        volume_classification=volume_classification,
                        brain_confidence=brain_confidence,
                    )
                )
            except Exception:
                continue

        return out, len(trades)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _group_stats(rows: List[TradeFeatureRow], key_name: str, key_fn) -> List[Dict[str, Any]]:
    groups: Dict[str, List[TradeFeatureRow]] = {}
    for r in rows:
        k = key_fn(r)
        if k is None:
            continue
        groups.setdefault(str(k), []).append(r)

    out: List[Dict[str, Any]] = []
    for k, items in groups.items():
        trades = len(items)
        if trades < 5:
            continue

        wins = [x.profit for x in items if x.profit > 0]
        losses = [x.profit for x in items if x.profit < 0]
        wr = 0.0
        if trades > 0:
            wr = round((len(wins) / trades) * 100.0, 1)
        pf = _compute_pf(wins, losses)
        insight = _insight_label(trades, wr, pf)

        out.append(
            {
                "dimension": key_name,
                "key": k,
                "trades": trades,
                "wr": wr,
                "pf": pf,
                "insight": insight,
            }
        )

    return out


def build_patterns_payload() -> Tuple[Dict[str, Any], Optional[str], Optional[str]]:
    rows, trade_count = _build_feature_rows()

    patterns_raw: List[Dict[str, Any]] = []

    patterns_raw.extend(
        _group_stats(
            rows,
            "session_direction",
            lambda r: f"{(r.session_name or 'UNKNOWN')} {r.direction}",
        )
    )

    patterns_raw.extend(
        _group_stats(
            rows,
            "rsi_bucket_direction",
            lambda r: (
                f"RSI {(_rsi_bucket(r.rsi_14) or 'UNKNOWN')} {r.direction}" if r.rsi_14 is not None else None
            ),
        )
    )

    patterns_raw.extend(
        _group_stats(
            rows,
            "mtf_alignment_direction",
            lambda r: f"MTF {(r.mtf_alignment or 'UNKNOWN')} {r.direction}",
        )
    )

    patterns_raw.extend(
        _group_stats(
            rows,
            "volume_bucket_direction",
            lambda r: (
                f"VOL {(_volume_bucket(r.volume_ratio) or 'UNKNOWN')} {r.direction}" if r.volume_ratio is not None else None
            ),
        )
    )

    patterns_raw.extend(
        _group_stats(
            rows,
            "confidence_bucket_direction",
            lambda r: (
                f"CONF {(_confidence_bucket(r.brain_confidence) or 'UNKNOWN')} {r.direction}" if r.brain_confidence is not None else None
            ),
        )
    )

    strong = [p for p in patterns_raw if p.get("insight") == "Strong edge"]
    avoid = [p for p in patterns_raw if str(p.get("insight", "")).startswith("Avoid")]

    strong.sort(key=lambda p: (p.get("pf", 0.0), p.get("trades", 0)), reverse=True)
    avoid.sort(key=lambda p: (p.get("pf", 999.0), -p.get("trades", 0)))

    selected: List[Dict[str, Any]] = []
    for p in strong[:20]:
        selected.append(p)
    for p in avoid[:20]:
        selected.append(p)

    if not selected:
        selected = patterns_raw[:20]

    def _render_name(p: Dict[str, Any]) -> str:
        k = str(p.get("key") or "").strip()
        if p.get("dimension") == "session_direction":
            return k.replace("UNKNOWN", "Session?")
        return k

    patterns_out: List[Dict[str, Any]] = []
    for p in selected[:40]:
        patterns_out.append(
            {
                "name": _render_name(p),
                "trades": int(p.get("trades", 0) or 0),
                "wr": float(p.get("wr", 0.0) or 0.0),
                "pf": float(p.get("pf", 0.0) or 0.0),
                "insight": str(p.get("insight") or "Neutral"),
            }
        )

    payload: Dict[str, Any] = {
        "updated": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "trade_count": int(trade_count),
        "patterns": patterns_out,
    }

    top_edge = None
    worst = None

    def _format_summary(item: Dict[str, Any]) -> str:
        return f"{item.get('name')}: {float(item.get('wr', 0.0)):.1f}% WR, PF {float(item.get('pf', 0.0)):.2f}, n={int(item.get('trades', 0))}"

    top_candidates = [x for x in patterns_out if x.get("insight") == "Strong edge"]
    if top_candidates:
        top_candidates.sort(key=lambda x: (x.get("pf", 0.0), x.get("trades", 0)), reverse=True)
        top_edge = _format_summary(top_candidates[0])

    worst_candidates = [x for x in patterns_out if str(x.get("insight", "")).startswith("Avoid")]
    if worst_candidates:
        worst_candidates.sort(key=lambda x: (x.get("pf", 999.0), -x.get("trades", 0)))
        worst = _format_summary(worst_candidates[0])

    return payload, top_edge, worst


def _patterns_file_path() -> str:
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_dir, _PATTERNS_FILE_REL)


def write_patterns_atomic(payload: Dict[str, Any]) -> bool:
    try:
        path = _patterns_file_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def read_patterns() -> Dict[str, Any]:
    path = _patterns_file_path()
    if not os.path.exists(path):
        return {"success": False, "reason": "patterns_missing"}
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if not isinstance(payload, dict):
            return {"success": False, "reason": "invalid_patterns"}
        return payload
    except Exception:
        return {"success": False, "reason": "invalid_patterns"}
