import sqlite3

conn = sqlite3.connect('data/history.db')

# Agent OPEN decisions Mar 12-13
print('=== AGENT OPEN DECISIONS (Mar 12-13) ===')
rows = conn.execute('''
    SELECT id, timestamp, agent_decision, agent_confidence, agent_reasoning, agent_key_factors, agent_concerns
    FROM agent_proactive_analyses 
    WHERE agent_decision IN ('OPEN_BUY', 'OPEN_SELL')
    AND timestamp >= '2026-03-12'
    AND timestamp < '2026-03-14'
    ORDER BY id
''').fetchall()
print(f'Total: {len(rows)}')
for r in rows:
    concerns = r[6][:200] if r[6] else 'NONE'
    print(f'\nID:{r[0]} | {r[1][:16]} | {r[2]} conf:{r[3]}%')
    print(f"  Reasoning: {r[4][:250] if r[4] else 'NONE'}")
    print(f'  Concerns: {concerns}')

# Trades Mar 12-13
print('\n\n=== TRADES (Mar 12-13) ===')
rows = conn.execute('''
    SELECT ticket, direction, open_price, close_price, profit, close_reason, 
           open_time, close_time
    FROM trades 
    WHERE close_time >= '2026-03-12'
    AND close_time < '2026-03-14'
    ORDER BY close_time
''').fetchall()
for r in rows:
    result = 'WIN' if r[4] is not None and r[4] >= 0.50 else ('LOSS' if r[4] is not None and r[4] <= -0.50 else 'BE')
    close_price = r[3] if r[3] is not None else 0
    profit = r[4] if r[4] is not None else 0
    open_t = r[6][:16] if r[6] else '?' 
    close_t = r[7][:16] if r[7] else '?' 
    print(f"  #{r[0]} {r[1]} | {r[2]:.2f} -> {close_price:.2f} | ${profit:+.2f} | {r[5]} | open:{open_t} close:{close_t} | {result}")

conn.close()
