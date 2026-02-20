"""
Post-first-cycle verification.
Run this after the market opens and the bot completes at least 1 analysis.
Usage: python scripts/verify_first_cycle.py
"""

import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

PASS = "\u2714"
FAIL = "\u2718"
WARN = "!"

def check(label, ok, detail=""):
    tag = PASS if ok else FAIL
    suffix = f" — {detail}" if detail else ""
    print(f"  [{tag}] {label}{suffix}")
    return ok


def main():
    results = []
    state_path = os.path.abspath(config.DASHBOARD_STATE_FILE)
    db_path = os.path.abspath(config.HISTORY_DB_PATH)

    print("=" * 60)
    print("  POST-FIRST-CYCLE VERIFICATION")
    print("=" * 60)

    # ── 1. Dashboard state JSON ──────────────────────────────
    print("\n1. Dashboard State (JSON)")
    state = None
    if os.path.exists(state_path):
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)

    results.append(check("bot_state.json exists", state is not None))

    if state:
        bot = state.get("bot", {})
        market = state.get("market", {})
        la = state.get("last_analysis", {})
        acc = state.get("account", {})

        results.append(check(
            "Bot OPERATIONAL",
            bot.get("status") == "OPERATIONAL",
            f"status={bot.get('status')}"
        ))
        results.append(check(
            "Market OPEN",
            market.get("is_open") is True,
            f"is_open={market.get('is_open')}, reason={market.get('reason')}"
        ))

        price = state.get("last_known_price")
        results.append(check(
            "Real price (tick)",
            price is not None and price > 0,
            f"price={price}"
        ))

        results.append(check(
            "Brain decision",
            la.get("decision") is not None,
            f"decision={la.get('decision')}, score={la.get('final_score')}, conf={la.get('confidence')}"
        ))

        pillars_ok = all(
            la.get(k) is not None
            for k in ("tech_score", "news_score", "ml_score", "momentum_score", "calendar_score")
        )
        results.append(check(
            "5 pillars with scores",
            pillars_ok,
            f"T={la.get('tech_score')} N={la.get('news_score')} ML={la.get('ml_score')} Mom={la.get('momentum_score')} Cal={la.get('calendar_score')}"
        ))

        results.append(check(
            "Account with balance",
            acc.get("balance") is not None and acc.get("balance", 0) > 0,
            f"balance={acc.get('balance')}, equity={acc.get('equity')}"
        ))

        positions = state.get("positions", [])
        if positions:
            print(f"\n  [INFO] {len(positions)} open position(s):")
            for p in positions:
                print(f"    #{p.get('ticket')} {p.get('direction')} {p.get('volume')} lot @ {p.get('open_price')} | P&L: ${p.get('profit', 0):.2f}")
        else:
            print(f"\n  [INFO] No open positions (normal if HOLD)")

    # ── 2. SQLite History DB ─────────────────────────────────
    print("\n2. SQLite History DB")
    results.append(check("history.db exists", os.path.exists(db_path)))

    if os.path.exists(db_path):
        conn = sqlite3.connect(db_path)
        c = conn.cursor()

        # Analyses
        n_analyses = c.execute("SELECT count(*) FROM analyses").fetchone()[0]
        results.append(check(
            "Table analyses has records",
            n_analyses > 0,
            f"count={n_analyses}"
        ))
        if n_analyses > 0:
            last = c.execute(
                "SELECT timestamp, decision, final_score, confidence, scenario FROM analyses ORDER BY id DESC LIMIT 1"
            ).fetchone()
            print(f"    Latest: {last[0]} | {last[1]} | score={last[2]} | conf={last[3]} | scenario={last[4]}")

        # Account snapshots
        n_snaps = c.execute("SELECT count(*) FROM account_snapshots").fetchone()[0]
        results.append(check(
            "Table account_snapshots accumulating",
            n_snaps > 0,
            f"count={n_snaps}"
        ))
        if n_snaps > 0:
            last_snap = c.execute(
                "SELECT timestamp, balance, equity, profit FROM account_snapshots ORDER BY id DESC LIMIT 1"
            ).fetchone()
            print(f"    Latest: {last_snap[0]} | bal={last_snap[1]} | eq={last_snap[2]} | profit={last_snap[3]}")

        # Trades
        n_trades = c.execute("SELECT count(*) FROM trades").fetchone()[0]
        if n_trades > 0:
            results.append(check("Table trades has records", True, f"count={n_trades}"))
            last_trade = c.execute(
                "SELECT ticket, direction, volume, open_price, close_price, profit, close_reason FROM trades ORDER BY id DESC LIMIT 1"
            ).fetchone()
            print(f"    Latest: #{last_trade[0]} {last_trade[1]} {last_trade[2]} lot @ {last_trade[3]} | close={last_trade[4]} | P&L={last_trade[5]} | reason={last_trade[6]}")
        else:
            print(f"  [{WARN}] Table trades empty (normal if no trade was opened)")

        conn.close()

    # ── Summary ──────────────────────────────────────────────────────
    passed = sum(1 for r in results if r)
    total = len(results)
    print("\n" + "=" * 60)
    print(f"  RESULT: {passed}/{total} checks passed")
    if passed == total:
        print("  All OK! Bot and SQLite working correctly.")
    else:
        print("  WARNING: Some checks failed. Check the logs.")
    print("=" * 60)


if __name__ == "__main__":
    main()
