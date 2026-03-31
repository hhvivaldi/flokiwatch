import sqlite3

conn = sqlite3.connect('data/history.db')
conn.row_factory = sqlite3.Row

close = conn.execute(
    """SELECT id, timestamp, agent_decision, agent_confidence,
              tp_entry_price, tp_stop_loss, tp_take_profit,
              close_reason, substr(raw_response,1,400) as raw
       FROM agent_proactive_analyses
       WHERE agent_decision='CLOSE_TRADE'
       ORDER BY id DESC
       LIMIT 1"""
).fetchone()

print('LATEST_CLOSE', dict(close) if close else None)

prev_open = None
if close:
    rows = conn.execute(
        """SELECT id, timestamp, agent_decision, agent_confidence,
                  tp_entry_price, tp_stop_loss, tp_take_profit
           FROM agent_proactive_analyses
           WHERE agent_decision IN ('OPEN_BUY','OPEN_SELL')
           ORDER BY id DESC
           LIMIT 50"""
    ).fetchall()

    for r in rows:
        if r['id'] < close['id']:
            prev_open = r
            break

print('PREV_OPEN', dict(prev_open) if prev_open else None)

conn.close()
