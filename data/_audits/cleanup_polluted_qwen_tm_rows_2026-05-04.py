"""FLO-419 (CEO 2026-05-04, Priority 3): clean QWEN_TM rows in
agent_events that were written by pytest runs of
flo403_phase2_trade_manager_test.py with hardcoded ticket=999 and
fixture SL/TP (4480/4520, 4490/4530, etc).

Identifier: author='QWEN_TM' AND payload_json contains '"ticket": 999'.
Real broker tickets on this account are 10-digit (e.g. 1626066712);
ticket=999 is unambiguous test pollution.

Confirms BEFORE/AFTER counts. Idempotent: running twice does nothing
the second time. Backs up the deleted rows to
data/_audits/agent_events_qwen_tm_pollution_backup_2026-05-04.csv
in case anyone wants to inspect what was removed.
"""
import csv
import os
import sqlite3
import sys

DB = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "data", "history.db"))
BACKUP = os.path.abspath(os.path.join(os.path.dirname(__file__), "agent_events_qwen_tm_pollution_backup_2026-05-04.csv"))
PREDICATE = "author='QWEN_TM' AND payload_json LIKE '%\"ticket\": 999%'"


def main():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    try:
        before = con.execute(f"SELECT COUNT(*) FROM agent_events WHERE {PREDICATE}").fetchone()[0]
        print(f"BEFORE: {before} polluted rows match predicate")
        if before == 0:
            print("Nothing to clean. Exiting.")
            return 0

        # Backup first
        rows = con.execute(
            f"SELECT id, timestamp, event_type, author, content, payload_json "
            f"FROM agent_events WHERE {PREDICATE} ORDER BY id"
        ).fetchall()
        with open(BACKUP, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["id", "timestamp", "event_type", "author", "content", "payload_json"])
            for r in rows:
                w.writerow([r["id"], r["timestamp"], r["event_type"], r["author"], r["content"], r["payload_json"]])
        print(f"backed up {len(rows)} rows -> {BACKUP}")

        # Delete
        con.execute(f"DELETE FROM agent_events WHERE {PREDICATE}")
        con.commit()

        after = con.execute(f"SELECT COUNT(*) FROM agent_events WHERE {PREDICATE}").fetchone()[0]
        print(f"AFTER:  {after} rows remain matching predicate")
        if after != 0:
            print("WARN: deletion incomplete; check predicate")
            return 1
        print(f"DELETED: {before - after} rows")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
