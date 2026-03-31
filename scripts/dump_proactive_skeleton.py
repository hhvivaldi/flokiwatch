import json
import sys
from pathlib import Path
from typing import Any, Dict, List


def _type_name(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, dict):
        return "dict"
    if isinstance(v, list):
        return "list"
    return type(v).__name__


def _dict_keys(v: Dict[str, Any]) -> List[str]:
    try:
        return list(v.keys())
    except Exception:
        return []


def _first_item_keys(v: List[Any]) -> List[str] | None:
    if not v:
        return None
    first = v[0]
    if isinstance(first, dict):
        return list(first.keys())
    return None


def build_skeleton(data: Dict[str, Any]) -> Dict[str, Any]:
    skeleton: Dict[str, Any] = {}

    for key in sorted(data.keys()):
        val = data[key]
        t = _type_name(val)

        if t == "dict":
            skeleton[key] = {
                "type": "dict",
                "keys": _dict_keys(val),
            }
        elif t == "list":
            skeleton[key] = {
                "type": "list",
                "length": len(val),
                "first_item_type": _type_name(val[0]) if val else None,
                "first_item_keys": _first_item_keys(val),
            }
        else:
            skeleton[key] = {"type": t}

    return skeleton


def main() -> int:
    path = Path("data/proactive_package_sample.json")
    if not path.exists():
        print("ERROR: data/proactive_package_sample.json not found.")
        print("- Run the bot until one PROACTIVE_H1 snapshot occurs.")
        return 2

    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception as e:
        print(f"ERROR: Failed to read/parse JSON: {e}")
        return 3

    if not isinstance(data, dict):
        print(f"ERROR: Expected top-level dict, got {type(data).__name__}")
        return 4

    skeleton = build_skeleton(data)
    print(json.dumps(skeleton, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
