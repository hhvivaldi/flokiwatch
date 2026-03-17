import json
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

import config
from logger import log
from agent_prompts import get_system_prompt


_ALLOWED_DECISIONS = {
    "WAIT",
    "OPEN_BUY",
    "OPEN_SELL",
    "HOLD_TRADE",
    "CLOSE_TRADE",
    "ADJUST_TRADE",
    "REJECT",
}


def _strip_think_tags(text: str) -> str:
    try:
        if not isinstance(text, str):
            return ""
        return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()
    except Exception:
        return "" if text is None else str(text)


def _safe_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        return float(v)
    except Exception:
        return None


def _safe_int(v: Any) -> Optional[int]:
    try:
        if v is None:
            return None
        return int(float(v))
    except Exception:
        return None


def _strip_code_fences(text: str) -> str:
    try:
        if not isinstance(text, str):
            return ""
        s = text.strip()
        if "```" not in s:
            return s
        # Extract the first fenced block if present; otherwise remove backticks.
        m = re.search(r"```(?:json)?\s*(.*?)\s*```", s, flags=re.DOTALL | re.IGNORECASE)
        if m:
            return (m.group(1) or "").strip()
        return s.replace("```", "").strip()
    except Exception:
        return "" if text is None else str(text)


def _first_json_object(text: str) -> Optional[Dict[str, Any]]:
    if not isinstance(text, str) or not text.strip():
        return None

    s = _strip_code_fences(text)
    if not s:
        return None

    # Fast path: pure JSON
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass

    # Extract a plausible JSON object by scanning brace pairs (best effort)
    try:
        starts = [i for i, ch in enumerate(s) if ch == "{"]
        ends = [i for i, ch in enumerate(s) if ch == "}"]
        if not starts or not ends:
            return None

        # Try smaller objects first (more likely to be the decision JSON)
        candidates: List[str] = []
        for i in starts:
            for j in ends:
                if j <= i:
                    continue
                # Limit candidate size to avoid huge captures
                if (j - i) > 8000:
                    continue
                candidates.append(s[i : j + 1])

        candidates.sort(key=len)
        for cand in candidates[:200]:
            try:
                obj = json.loads(cand)
                if isinstance(obj, dict):
                    return obj
            except Exception:
                continue

        return None
    except Exception:
        return None


def _fmt_num(v: Any, digits: int = 2) -> str:
    try:
        n = float(v)
        if n != n:
            return "—"
        return f"{n:.{digits}f}"
    except Exception:
        return "—"


def _get_nested(d: Dict[str, Any], keys: List[str]) -> Any:
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def _extract_sr_nearest_10(agent_data: Dict[str, Any], mid_price: Optional[float]) -> List[Dict[str, Any]]:
    try:
        sr = agent_data.get("sr_zones") or agent_data.get("sr") or {}
        zones = None
        if isinstance(sr, dict):
            zones = sr.get("zones") or sr.get("nearest") or sr.get("levels")
        if not isinstance(zones, list) or not zones:
            return []

        def center(z: Dict[str, Any]) -> Optional[float]:
            if not isinstance(z, dict):
                return None
            lo = _safe_float(z.get("low"))
            hi = _safe_float(z.get("high"))
            if lo is not None and hi is not None:
                return (lo + hi) / 2.0
            lvl = _safe_float(z.get("level"))
            if lvl is not None:
                return lvl
            lvl = _safe_float(z.get("price"))
            if lvl is not None:
                return lvl
            return None

        if mid_price is None:
            out = [z for z in zones if isinstance(z, dict)][:10]
            return out

        scored: List[Tuple[float, Dict[str, Any]]] = []
        for z in zones:
            if not isinstance(z, dict):
                continue
            c = center(z)
            if c is None:
                continue
            scored.append((abs(float(c) - float(mid_price)), z))
        scored.sort(key=lambda x: x[0])
        return [z for _, z in scored[:10]]
    except Exception:
        return []


def build_shadow_prompt(agent_data: Dict[str, Any]) -> str:
    dp = agent_data if isinstance(agent_data, dict) else {}

    try:
        log.debug(
            f"SHADOW_PROMPT_DEBUG | positions_key_exists={('positions' in dp)} | "
            f"open_positions_key={('open_positions' in dp)} | keys={list(dp.keys())[:10]}"
        )
    except Exception:
        pass

    # Price
    bid = _safe_float(_get_nested(dp, ["current_price", "bid"]))
    ask = _safe_float(_get_nested(dp, ["current_price", "ask"]))
    spread = _safe_float(_get_nested(dp, ["current_price", "spread"]))
    mid = None
    try:
        if bid is not None and ask is not None:
            mid = (bid + ask) / 2.0
    except Exception:
        mid = None

    # Indicators
    ind = dp.get("indicators") if isinstance(dp.get("indicators"), dict) else {}

    rsi = _safe_float(_get_nested(ind, ["rsi", "value"]))
    macd = _safe_float(_get_nested(ind, ["macd", "value"]))
    macd_hist = _safe_float(_get_nested(ind, ["macd", "hist"]))
    adx = _safe_float(_get_nested(ind, ["adx", "value"]))
    atr = _safe_float(_get_nested(ind, ["atr", "value"]))

    emas = ind.get("emas") if isinstance(ind.get("emas"), dict) else {}
    ema50 = _safe_float(emas.get("ema50"))
    ema200 = _safe_float(emas.get("ema200"))

    bb = ind.get("bollinger") if isinstance(ind.get("bollinger"), dict) else {}
    bb_pos = _safe_float(bb.get("position"))
    bb_upper = _safe_float(bb.get("upper"))
    bb_middle = _safe_float(bb.get("middle"))
    bb_lower = _safe_float(bb.get("lower"))

    # Positions
    positions = dp.get("open_positions")
    if not isinstance(positions, list):
        positions = dp.get("positions")
    if not isinstance(positions, list):
        positions = []

    # Macro
    macro = dp.get("macro") if isinstance(dp.get("macro"), dict) else {}
    dxy = _safe_float(_get_nested(macro, ["dxy", "value"]))
    vix = _safe_float(_get_nested(macro, ["vix", "value"]))

    y10 = None
    try:
        y = macro.get("yields")
        if isinstance(y, dict):
            y10 = _safe_float(y.get("value") or y.get("us10y") or y.get("10y"))
    except Exception:
        y10 = None

    # Headlines
    headlines = dp.get("headlines") or dp.get("news_headlines")
    if not isinstance(headlines, list):
        headlines = []
    headlines = [str(h).strip() for h in headlines if h is not None and str(h).strip()][:5]

    # ML
    ml = dp.get("ml") if isinstance(dp.get("ml"), dict) else {}
    ml_dir = ml.get("direction") or ml.get("prediction")
    ml_prob = ml.get("h1_prob") or ml.get("probability") or ml.get("confidence")

    # Calendar
    cal = dp.get("calendar") if isinstance(dp.get("calendar"), dict) else {}
    cal_score = _safe_float(cal.get("score"))
    cal_phase = cal.get("phase")
    cal_bias = cal.get("bias")
    cal_events = cal.get("events")
    if not isinstance(cal_events, list):
        cal_events = []

    # SR zones (nearest 10)
    sr_nearest = _extract_sr_nearest_10(dp, mid_price=mid)

    lines: List[str] = []
    lines.append("You are running in SHADOW MODE. You have NO tool access. Use only the data below.")
    lines.append("Return ONLY valid JSON: {\"decision\":..., \"confidence\":0-100, \"reasoning\":\"...\"}.")
    lines.append("Decisions allowed: WAIT, OPEN_BUY, OPEN_SELL, HOLD_TRADE, CLOSE_TRADE.")
    lines.append("")

    lines.append("# PRICE")
    lines.append(f"bid={_fmt_num(bid, 2)} ask={_fmt_num(ask, 2)} spread={_fmt_num(spread, 2)} mid={_fmt_num(mid, 2)}")

    lines.append("\n# INDICATORS")
    lines.append(
        " ".join(
            [
                f"RSI14={_fmt_num(rsi, 1)}",
                f"MACD={_fmt_num(macd, 2)}",
                f"MACD_hist={_fmt_num(macd_hist, 2)}",
                f"ADX14={_fmt_num(adx, 1)}",
                f"ATR14={_fmt_num(atr, 1)}",
                f"EMA50={_fmt_num(ema50, 2)}",
                f"EMA200={_fmt_num(ema200, 2)}",
                f"BB_pos={_fmt_num(bb_pos, 2)}",
                f"BB_upper={_fmt_num(bb_upper, 2)}",
                f"BB_mid={_fmt_num(bb_middle, 2)}",
                f"BB_low={_fmt_num(bb_lower, 2)}",
            ]
        )
    )

    lines.append("\n# OPEN POSITIONS")
    if not positions:
        lines.append("none")
    else:
        for p in positions[:5]:
            if not isinstance(p, dict):
                continue
            ticket = p.get("ticket")
            direction = p.get("direction") or p.get("type")
            entry = p.get("open_price") or p.get("entry")
            pnl = p.get("profit")
            sl = p.get("sl")
            tp = p.get("tp")
            lines.append(
                f"#{ticket} {direction} entry={_fmt_num(entry, 2)} pnl={_fmt_num(pnl, 2)} sl={_fmt_num(sl, 2)} tp={_fmt_num(tp, 2)}"
            )

    lines.append("\n# SUPPORT/RESISTANCE (nearest 10)")
    if not sr_nearest:
        lines.append("none")
    else:
        for z in sr_nearest:
            if not isinstance(z, dict):
                continue
            ztype = z.get("type") or z.get("kind") or "zone"
            lo = z.get("low")
            hi = z.get("high")
            lvl = z.get("level") or z.get("price")
            touches = z.get("touches")
            if lo is not None and hi is not None:
                lines.append(f"{ztype}: { _fmt_num(lo,2)}-{_fmt_num(hi,2)} touches={touches}")
            elif lvl is not None:
                lines.append(f"{ztype}: level={_fmt_num(lvl,2)} touches={touches}")

    lines.append("\n# MACRO")
    lines.append(f"DXY={_fmt_num(dxy, 2)} VIX={_fmt_num(vix, 2)} US10Y={_fmt_num(y10, 2)}")

    lines.append("\n# HEADLINES (top 5)")
    if not headlines:
        lines.append("none")
    else:
        for h in headlines:
            lines.append(f"- {h}")

    lines.append("\n# ML")
    lines.append(f"direction={str(ml_dir) if ml_dir is not None else '—'} prob={str(ml_prob) if ml_prob is not None else '—'}")

    lines.append("\n# CALENDAR")
    lines.append(
        f"score={_fmt_num(cal_score, 1)} phase={str(cal_phase) if cal_phase is not None else '—'} bias={str(cal_bias) if cal_bias is not None else '—'}"
    )
    if cal_events:
        for ev in cal_events[:5]:
            if isinstance(ev, dict):
                title = ev.get("title") or ev.get("event") or "event"
                impact = ev.get("impact") or ev.get("importance")
                when = ev.get("time") or ev.get("timestamp")
                lines.append(f"- {title} impact={impact} time={when}")
            else:
                lines.append(f"- {str(ev)}")

    if positions:
        lines.append("\nIMPORTANT CONTEXT: You currently have the following open position(s):")
        for p in positions[:5]:
            if not isinstance(p, dict):
                continue
            ticket = p.get("ticket")
            direction = p.get("direction") or p.get("type")
            entry = p.get("open_price") or p.get("entry")
            pnl = p.get("profit")
            sl = p.get("sl")
            tp = p.get("tp")
            lines.append(
                f"- #{ticket} {direction} entry={_fmt_num(entry, 2)} pnl={_fmt_num(pnl, 2)} sl={_fmt_num(sl, 2)} tp={_fmt_num(tp, 2)}"
            )
        lines.append("Your decision must be one of:")
        lines.append("- HOLD_TRADE: keep the position open")
        lines.append("- CLOSE_TRADE: close the position now")
        lines.append("- ADJUST_TRADE: modify SL or TP")
        lines.append("Do NOT respond with WAIT or OPEN when you have an open position. WAIT and OPEN are only valid when you have NO open positions.")
    else:
        lines.append("\nYou have no open positions. Your decision must be one of:")
        lines.append("- WAIT: do nothing")
        lines.append("- OPEN_BUY: open a buy position")
        lines.append("- OPEN_SELL: open a sell position")

    return "\n".join(lines).strip()


def call_shadow_model(agent_data: Dict[str, Any], floki_decision: Dict[str, Any]) -> Dict[str, Any]:
    t0 = time.time()
    out: Dict[str, Any] = {
        "decision": None,
        "confidence": None,
        "reasoning": None,
        "latency_ms": 0,
        "error": None,
    }

    try:
        _ = floki_decision
        if not getattr(config, "SHADOW_MODEL_ENABLED", False):
            out["error"] = "disabled"
            return out

        url = str(getattr(config, "SHADOW_MODEL_URL", "") or "").strip()
        model = str(getattr(config, "SHADOW_MODEL_NAME", "") or "").strip()
        timeout_s = int(getattr(config, "SHADOW_MODEL_TIMEOUT", 120) or 120)

        if not url:
            out["error"] = "missing_url"
            return out
        if not model:
            out["error"] = "missing_model"
            return out

        user_prompt = build_shadow_prompt(agent_data)

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": get_system_prompt()},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
        }

        resp = requests.post(url, json=payload, timeout=timeout_s)
        out["latency_ms"] = int((time.time() - t0) * 1000)

        if resp.status_code >= 400:
            out["error"] = f"http_{resp.status_code}"
            return out

        try:
            data = resp.json()
        except Exception:
            out["error"] = "invalid_json_response"
            return out

        content = None
        try:
            choices = data.get("choices")
            if isinstance(choices, list) and choices:
                msg = choices[0].get("message") if isinstance(choices[0], dict) else None
                if isinstance(msg, dict):
                    content = msg.get("content")
        except Exception:
            content = None

        if not isinstance(content, str) or not content.strip():
            out["error"] = "empty_content"
            return out

        cleaned = _strip_think_tags(content)
        obj = _first_json_object(cleaned)
        if not isinstance(obj, dict):
            try:
                raw_preview = str(content).replace("\r", " ").replace("\n", " ")
                raw_preview = raw_preview[:500]
                log.info(f"SHADOW_RAW | first_500_chars: {raw_preview}")
                out["raw_preview"] = raw_preview
            except Exception:
                pass
            out["error"] = "invalid_decision_json"
            return out

        decision = str(obj.get("decision") or "").strip().upper()
        conf = _safe_int(obj.get("confidence"))
        reasoning = str(obj.get("reasoning") or "").strip()

        if decision not in _ALLOWED_DECISIONS:
            out["error"] = f"invalid_decision:{decision or 'missing'}"
            return out

        if conf is not None:
            conf = max(0, min(100, conf))

        out["decision"] = decision
        out["confidence"] = conf
        out["reasoning"] = reasoning[:2000] if reasoning else ""
        return out

    except requests.Timeout:
        out["latency_ms"] = int((time.time() - t0) * 1000)
        out["error"] = "timeout"
        return out
    except Exception as e:
        out["latency_ms"] = int((time.time() - t0) * 1000)
        out["error"] = str(e)
        try:
            log.debug(f"SHADOW_MODEL | error (non-blocking): {e}")
        except Exception:
            pass
        return out
