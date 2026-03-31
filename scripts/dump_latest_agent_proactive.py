import json
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple


DB_PATH = "data/history.db"
TABLE = "agent_proactive_analyses"


def _get_columns(cur: sqlite3.Cursor, table: str) -> List[str]:
    cur.execute(f"PRAGMA table_info({table})")
    rows = cur.fetchall()
    return [r[1] for r in rows]


def _safe_preview(value: Any, limit: int = 2000) -> str:
    if value is None:
        return "<NULL>"
    if isinstance(value, (bytes, bytearray)):
        return f"<BLOB {len(value)} bytes>"
    s = str(value)
    if len(s) > limit:
        return s[:limit] + f"\n<TRUNCATED: {len(s) - limit} chars omitted>"
    return s


def _pick_column(columns: List[str], candidates: List[str]) -> Optional[str]:
    lower_map = {c.lower(): c for c in columns}
    for cand in candidates:
        if cand.lower() in lower_map:
            return lower_map[cand.lower()]
    return None


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()

        columns = _get_columns(cur, TABLE)
        if not columns:
            raise RuntimeError(f"No columns found for table {TABLE}")

        id_col = _pick_column(columns, ["id", "rowid"])
        created_col = _pick_column(columns, ["created_at", "timestamp", "ts", "created", "time"])

        order_by = None
        if created_col:
            order_by = f"{created_col} DESC"
        elif id_col and id_col != "rowid":
            order_by = f"{id_col} DESC"
        else:
            order_by = "rowid DESC"

        cur.execute(f"SELECT * FROM {TABLE} ORDER BY {order_by} LIMIT 1")
        row = cur.fetchone()
        if row is None:
            print(f"No rows found in {TABLE}")
            return

        raw_col = _pick_column(columns, ["raw_response", "raw", "agent_raw_response", "response_raw"])
        tool_trace_col = _pick_column(
            columns,
            [
                "tool_trace",
                "tool_calls",
                "tool_loop",
                "tool_log",
                "tools",
                "tool_history",
                "agent_tool_trace",
            ],
        )

        print("=== LATEST agent_proactive_analyses ROW ===")
        if id_col and id_col in row.keys():
            print(f"{id_col}: {row[id_col]}")
        if created_col and created_col in row.keys():
            print(f"{created_col}: {row[created_col]}")

        print("\n=== COLUMNS (preview) ===")
        preview_keys = []
        for k in row.keys():
            lk = k.lower()
            if lk in {"raw_response", "tool_trace"}:
                continue
            if lk.endswith("response") or lk.endswith("trace"):
                continue
            preview_keys.append(k)

        for k in preview_keys:
            try:
                v = row[k]
            except Exception:
                continue
            # Avoid printing huge JSON blobs here; keep it readable
            print(f"- {k}: {_safe_preview(v, limit=250)}")

        if tool_trace_col:
            print("\n=== TOOL_TRACE (full, may be truncated in terminal) ===")
            print(_safe_preview(row[tool_trace_col], limit=30000))
        else:
            print("\n=== TOOL_TRACE ===")
            print("<No tool_trace-like column found in this table>")

        if raw_col:
            print("\n=== RAW_RESPONSE (full, may be truncated in terminal) ===")
            print(_safe_preview(row[raw_col], limit=30000))
        else:
            print("\n=== RAW_RESPONSE ===")
            print("<No raw_response-like column found in this table>")

        print("\n=== TABLE INFO ===")
        print(f"DB: {DB_PATH}")
        print(f"Table: {TABLE}")
        print(f"OrderBy: {order_by}")
        print(f"Detected raw_response column: {raw_col or '<none>'}")
        print(f"Detected tool_trace column: {tool_trace_col or '<none>'}")
        print(f"Total columns: {len(columns)}")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
