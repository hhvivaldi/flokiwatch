import sqlite3, json

conn = sqlite3.connect("data/history.db")
cur = conn.cursor()
cur.execute("SELECT raw_response FROM agent_proactive_analyses WHERE agent_decision='OPEN_BUY' ORDER BY timestamp DESC LIMIT 1")
row = cur.fetchone()

if row:
    d = json.loads(row[0])
    tp = d.get("trade_plan") or d.get("entry_conditions") or {}
    print("breakeven_trigger:", tp.get("breakeven_trigger"))
    print("trailing_trigger:", tp.get("trailing_trigger"))
    print("trailing_distance:", tp.get("trailing_distance"))
    print("management_mode:", tp.get("management_mode"))
    print("all keys:", list(tp.keys()) if isinstance(tp, dict) else "N/A")
else:
    print("No OPEN_BUY found")

conn.close()
