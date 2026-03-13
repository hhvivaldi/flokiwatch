import json
import os
import sys
import time
from datetime import datetime, timedelta

try:
    import MetaTrader5 as mt5
except Exception:
    mt5 = None


def _safe_float(x):
    try:
        return float(x)
    except Exception:
        return None


def _safe_iso(dt):
    if dt is None:
        return None
    try:
        return dt.isoformat()
    except Exception:
        try:
            return str(dt)
        except Exception:
            return None


def _write_json(path: str, payload: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, default=str)
        os.replace(tmp, path)
    except Exception:
        pass


def _parse_ticket(argv) -> int:
    if len(argv) < 2:
        raise ValueError("Missing ticket argument")
    try:
        return int(str(argv[1]).strip())
    except Exception as e:
        raise ValueError(f"Invalid ticket: {argv[1]}") from e


def _find_deal_by_ticket(deals, ticket: int):
    if not deals:
        return None
    for d in deals:
        try:
            if int(getattr(d, "ticket", 0) or 0) == int(ticket):
                return d
        except Exception:
            continue
    return None


def main(argv) -> int:
    ticket = None
    out_path = os.path.join("data", "deal_resolved.json")

    try:
        ticket = _parse_ticket(argv)
    except Exception:
        _write_json(out_path, {"ticket": None, "resolved": False})
        return 2

    if mt5 is None:
        _write_json(out_path, {"ticket": ticket, "resolved": False})
        return 3

    try:
        mt5.shutdown()
    except Exception:
        pass

    initialized = False
    try:
        initialized = bool(mt5.initialize())
    except Exception:
        initialized = False

    if not initialized:
        _write_json(out_path, {"ticket": ticket, "resolved": False})
        try:
            mt5.shutdown()
        except Exception:
            pass
        return 4

    try:
        now = datetime.now()
        start = now - timedelta(days=7)

        found = None
        last_err = None
        for _attempt in range(3):
            try:
                deals = mt5.history_deals_get(start, now)
                found = _find_deal_by_ticket(deals, ticket)
            except Exception as e:
                last_err = str(e)
                found = None

            if found is not None:
                profit = _safe_float(getattr(found, "profit", None))
                close_price = _safe_float(getattr(found, "price", None))
                close_time = getattr(found, "time", None)

                payload = {
                    "ticket": ticket,
                    "resolved": True,
                    "profit": profit,
                    "close_price": close_price,
                    "close_time": _safe_iso(close_time),
                    "reason": "resolved_by_subprocess",
                }
                _write_json(out_path, payload)
                return 0

            time.sleep(2.0)

        payload = {"ticket": ticket, "resolved": False}
        if last_err:
            payload["error"] = last_err
        _write_json(out_path, payload)
        return 1

    finally:
        try:
            mt5.shutdown()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main(sys.argv))
