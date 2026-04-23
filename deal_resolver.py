import json
import os
import sys
import time
from datetime import datetime, timedelta

try:
    from mt5_safe import mt5  # FLO-348
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


def _safe_int(x):
    try:
        return int(x)
    except Exception:
        return None


def _is_close_deal(d) -> bool:
    try:
        entry = getattr(d, "entry", None)
        if entry is None:
            return False

        entry_i = _safe_int(entry)
        if entry_i is not None:
            # MT5: DEAL_ENTRY_OUT = 1
            return entry_i == 1

        # Fallback if enum name is present
        entry_s = str(entry).upper()
        return "OUT" in entry_s
    except Exception:
        return False


def _find_close_deal_by_position_id(deals, position_id: int):
    if not deals:
        return None
    found = None
    for d in deals:
        try:
            pos_id = getattr(d, "position_id", None)
            if pos_id is None:
                continue
            if int(pos_id) != int(position_id):
                continue
            if not _is_close_deal(d):
                continue
            found = d
            break
        except Exception:
            continue
    return found


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

        for _attempt in range(5):
            try:
                now = datetime.now()
                deals = mt5.history_deals_get(start, now)
                found = _find_close_deal_by_position_id(deals, ticket)
            except Exception as e:
                last_err = str(e)
                found = None

            if found is not None:
                profit = _safe_float(getattr(found, "profit", None))
                close_price = _safe_float(getattr(found, "price", None))
                close_time = getattr(found, "time", None)

                payload = {
                    "ticket": ticket,
                    "position_id": ticket,
                    "resolved": True,
                    "profit": profit,
                    "close_price": close_price,
                    "close_time": _safe_iso(close_time),
                    "reason": "resolved_by_subprocess",
                }
                _write_json(out_path, payload)
                return 0

            time.sleep(15.0)

        payload = {"ticket": ticket, "position_id": ticket, "resolved": False}
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
