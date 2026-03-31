import sqlite3
from pathlib import Path

DB_PATH = Path(r"C:\Users\Hermano\OneDrive\Desktop\XAUUSD\data\history.db")


def main() -> None:
    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}")
        return

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT
              id,
              timestamp,
              h1_close_time,
              agent_decision,
              agent_confidence,
              substr(raw_response, 1, 500) AS raw_prefix,
              tp_entry_strategy,
              tp_entry_price,
              tp_stop_loss,
              tp_take_profit,
              tp_risk_reward_ratio
            FROM agent_proactive_analyses
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

        if not row:
            print("NO ROWS")
            return

        payload = dict(row)
        print(payload)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
