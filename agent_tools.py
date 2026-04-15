import json
import os
import time
from datetime import datetime, timedelta
from tz_utils import utc_iso, utc_now  # FLO-286 / FLO-309
from typing import Any, Dict, Optional, List, Tuple

from logger import log


# FLO-141: per-ticket adjustment rate limiter (in-memory, lost on restart)
_adjust_rate_history: Dict[int, List[float]] = {}


class AgentTools:
    def __init__(
        self,
        bot: Any,
        *,
        executor: Any,
        safety_checks_module: Any,
        risk_manager_module: Any,
    ):
        self._bot = bot
        self._executor = executor
        self._safety = safety_checks_module
        self._risk = risk_manager_module

    def set_next_check(self, minutes: int = 5) -> Dict[str, Any]:
        start = time.time()
        try:
            m = self._safe_int(minutes)
            if m is None:
                m = 5
            if m < 2:
                m = 2
            if m > 120:
                m = 120

            # Position mode cap: max 2 min with open position
            _capped = False
            _requested = int(m)
            try:
                import config as _cfg_snc
                _max_pos = int(getattr(_cfg_snc, "FLOKI_MAX_CHECK_WITH_POSITION", 10) or 10)
                _positions = self._executor.get_open_positions() if self._executor else []
                if _positions and m > _max_pos:
                    m = _max_pos
                    _capped = True
            except Exception:
                pass

            now = datetime.utcnow()
            next_at = now + timedelta(minutes=int(m))
            payload = {
                "next_check_at": next_at.isoformat(timespec="seconds") + "Z",
                "requested_minutes": int(m),
            }

            ok = self._write_json_atomic(self._next_check_path(), payload)
            if not ok:
                self._log_fail("set_next_check", start, "persist failed")
                return {"success": False, "reason": "persist failed"}

            self._log_tool("set_next_check", start, f"minutes={m}")
            result = {"success": True, **payload}
            if _capped:
                result["capped"] = True
                result["original_requested"] = _requested
                result["reason"] = f"Position open — capped from {_requested} to {m} minutes"
            return result
        except Exception as e:
            self._log_tool("set_next_check", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    # ---------------------------------------------------------------------
    # Internals
    # ---------------------------------------------------------------------

    def _no_cache(self) -> Dict[str, Any]:
        return {"success": False, "reason": "no cached data available"}

    def _last_agent_data(self) -> Optional[Dict[str, Any]]:
        try:
            dp = getattr(self._bot, "_last_agent_data", None)
            return dp if isinstance(dp, dict) and dp else None
        except Exception:
            return None

    def _nearest_sr_zones(self, zones: List[Dict[str, Any]], mid_price: float, limit: int = 5) -> List[Dict[str, Any]]:
        try:
            def zone_center(z: Dict[str, Any]) -> Optional[float]:
                a = self._safe_float(z.get("low"))
                b = self._safe_float(z.get("high"))
                if a is not None and b is not None:
                    return (a + b) / 2.0
                c = self._safe_float(z.get("level"))
                if c is not None:
                    return c
                c = self._safe_float(z.get("price"))
                if c is not None:
                    return c
                return None

            scored: List[Tuple[float, Dict[str, Any]]] = []
            for z in zones:
                if not isinstance(z, dict):
                    continue
                c = zone_center(z)
                if c is None:
                    continue
                scored.append((abs(float(c) - float(mid_price)), z))
            scored.sort(key=lambda x: x[0])
            return [z for _, z in scored[: max(1, int(limit))]]
        except Exception:
            return zones[: max(1, int(limit))] if isinstance(zones, list) else []

    def _extract_ema50_ema200(self, dp: Dict[str, Any]) -> Dict[str, Any]:
        out = {"ema50": None, "ema200": None}
        try:
            ind = dp.get("indicators") or {}
            emas = ind.get("emas") or {}
            if isinstance(emas, dict):
                out["ema50"] = self._safe_float(emas.get("ema50"))
                out["ema200"] = self._safe_float(emas.get("ema200"))
        except Exception:
            return out
        return out

    def _extract_recent_candles_for_rex(self, dp: Dict[str, Any]) -> Dict[str, Any]:
        out = {"H1_last5": [], "M5_last3": [], "volume_context": {"H1_last": None, "M5_last3": []}}
        try:
            cds = dp.get("candles") or {}
            if isinstance(cds, dict):
                h1 = cds.get("H1")
                m5 = cds.get("M5")
                if isinstance(h1, list) and h1:
                    out["H1_last5"] = h1[-5:]
                if isinstance(m5, list) and m5:
                    out["M5_last3"] = m5[-3:]
        except Exception:
            pass

        try:
            if not out["H1_last5"]:
                built = self.get_candles("H1", 5)
                if isinstance(built, dict) and isinstance(built.get("candles"), list):
                    out["H1_last5"] = built.get("candles")[-5:]
        except Exception:
            pass

        try:
            if not out["M5_last3"]:
                built = self.get_candles("M5", 3)
                if isinstance(built, dict) and isinstance(built.get("candles"), list):
                    out["M5_last3"] = built.get("candles")[-3:]
        except Exception:
            pass

        try:
            if isinstance(out.get("H1_last5"), list) and out["H1_last5"]:
                out["volume_context"]["H1_last"] = out["H1_last5"][-1].get("volume")
        except Exception:
            pass

        try:
            vols = []
            for c in out.get("M5_last3") or []:
                if isinstance(c, dict):
                    vols.append(c.get("volume"))
            out["volume_context"]["M5_last3"] = vols
        except Exception:
            pass

        return out

    def _last_df(self) -> Any:
        try:
            return getattr(self._bot, "_last_df", None)
        except Exception:
            return None

    def _now_iso(self) -> str:
        return datetime.utcnow().isoformat(timespec="seconds") + "Z"

    def _safe_float(self, v: Any) -> Optional[float]:
        try:
            if v is None:
                return None
            return float(v)
        except Exception:
            return None

    def _safe_int(self, v: Any) -> Optional[int]:
        try:
            if v is None:
                return None
            return int(v)
        except Exception:
            return None

    def _infer_session_from_utc_hour(self, utc_hour: Optional[int]) -> Optional[str]:
        if utc_hour is None:
            return None
        try:
            h = int(utc_hour) % 24
        except Exception:
            return None
        if 0 <= h <= 6:
            return "ASIAN"
        if 7 <= h <= 12:
            return "LONDON"
        if 13 <= h <= 20:
            return "NY"
        return "OFF"

    def _build_session_context_for_rex(
        self, session_name: Optional[str], indicators: Dict[str, Any], dp: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Build session context so Rex can evaluate data in session context."""
        ctx: Dict[str, Any] = {"name": session_name}
        try:
            utc_hour = self._safe_int(dp.get("utc_hour"))
            if utc_hour is None:
                utc_hour = datetime.utcnow().hour
            ctx["utc_hour"] = utc_hour

            # Hours into session
            session_starts = {"ASIAN": 0, "LONDON": 7, "NY": 13, "OFF": 21}
            start = session_starts.get(session_name or "", 0)
            ctx["hours_into_session"] = (utc_hour - start) % 24

            # Volume ratio vs average (from indicators if available)
            vol = indicators.get("volume") if isinstance(indicators, dict) else None
            if isinstance(vol, dict):
                ctx["volume_ratio"] = vol.get("tick_volume_ratio")
                ctx["volume_classification"] = vol.get("classification")
        except Exception:
            pass
        return ctx

    def _rsi_bucket(self, rsi: Optional[float]) -> Optional[str]:
        if rsi is None:
            return None
        try:
            v = float(rsi)
        except Exception:
            return None
        if v < 30:
            return "<30"
        if v < 40:
            return "30-40"
        if v <= 60:
            return "40-60"
        if v <= 70:
            return "60-70"
        return ">70"

    def _extract_context_for_patterns(self) -> Dict[str, Any]:
        dp = self._last_agent_data() or {}

        direction = None
        session = None
        rsi = None

        try:
            direction = dp.get("decision")
            if isinstance(direction, str) and direction.upper() in ("BUY", "SELL"):
                direction = direction.upper()
            else:
                direction = None
        except Exception:
            direction = None

        try:
            session = dp.get("session_name")
            if not isinstance(session, str) or not session.strip():
                session = None
            else:
                session = session.strip().upper()
        except Exception:
            session = None

        if session is None:
            try:
                utc_hour = self._safe_int(dp.get("utc_hour"))
                session = self._infer_session_from_utc_hour(utc_hour)
            except Exception:
                session = None

        try:
            ind = dp.get("indicators") or {}
            rsi_blob = ind.get("rsi") or {}
            rsi = self._safe_float(rsi_blob.get("value"))
        except Exception:
            rsi = None

        return {
            "direction": direction,
            "session": session,
            "rsi": rsi,
            "rsi_bucket": self._rsi_bucket(rsi),
        }

    def _get_connection(self):
        import sqlite3
        import config

        db_path = os.path.abspath(getattr(config, "HISTORY_DB_PATH", "data/history.db"))
        conn = sqlite3.connect(db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        return conn

    def _find_nearest_analysis(self, conn, open_time: str):
        from datetime import datetime

        if not open_time:
            return None
        try:
            cur = conn.execute(
                """
                SELECT timestamp, utc_hour, session_name, rsi_14
                FROM analyses
                WHERE timestamp <= ?
                ORDER BY timestamp DESC
                LIMIT 1
                """,
                (open_time,),
            )
            row = cur.fetchone()
            if not row:
                return None

            t_trade = datetime.fromisoformat(str(open_time).replace("Z", ""))
            t_ana = datetime.fromisoformat(str(row["timestamp"]).replace("Z", ""))
            gap = (t_trade - t_ana).total_seconds()
            if gap < 0 or gap > 5 * 60:
                return None
            return row
        except Exception:
            return None

    def _query_similar_losing_trades(self, context: Dict[str, Any], limit: int = 2) -> List[Dict[str, Any]]:
        direction = str(context.get("direction") or "").upper().strip()
        session = str(context.get("session") or "").upper().strip()
        rsi_bucket = context.get("rsi_bucket")

        if direction not in ("BUY", "SELL"):
            return []

        conn = None
        try:
            conn = self._get_connection()
            cur = conn.execute(
                """
                SELECT ticket, direction, profit, open_price, close_price, open_time, close_time, close_reason
                FROM trades
                WHERE close_time IS NOT NULL
                  AND profit IS NOT NULL
                  AND profit < 0
                  AND UPPER(direction) = ?
                  AND decision_source IN ('floki_agent', 'agent_floki')
                ORDER BY close_time DESC
                LIMIT 50
                """,
                (direction,),
            )
            candidates = list(cur.fetchall() or [])

            filtered: List[Dict[str, Any]] = []
            for tr in candidates:
                try:
                    open_time = str(tr["open_time"] or "")
                    a = self._find_nearest_analysis(conn, open_time=open_time)

                    a_session = None
                    a_rsi = None
                    if a is not None:
                        a_session = str(a["session_name"] or "").strip().upper() or None
                        a_rsi = self._safe_float(a["rsi_14"])
                        if a_session is None:
                            a_session = self._infer_session_from_utc_hour(self._safe_int(a["utc_hour"]))

                    if session and a_session and session != a_session:
                        continue

                    if rsi_bucket and a_rsi is not None:
                        if self._rsi_bucket(a_rsi) != rsi_bucket:
                            continue

                    filtered.append(
                        {
                            "ticket": int(tr["ticket"] or 0),
                            "direction": str(tr["direction"] or ""),
                            "profit": float(tr["profit"] or 0.0),
                            "open_price": self._safe_float(tr["open_price"]),
                            "close_price": self._safe_float(tr["close_price"]),
                            "open_time": open_time,
                            "close_time": str(tr["close_time"] or ""),
                            "close_reason": str(tr["close_reason"] or ""),
                            "session": a_session,
                            "rsi_14": a_rsi,
                        }
                    )

                    if len(filtered) >= int(limit):
                        break
                except Exception:
                    continue

            return filtered
        except Exception:
            return []
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _pip_size(self) -> float:
        return 0.1

    def _sl_pips_from_prices(self, entry: float, sl: float) -> Optional[float]:
        try:
            pip = self._pip_size()
            return abs(entry - sl) / pip
        except Exception:
            return None

    def _extract_price_from_cache(self, dp: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        try:
            cp = dp.get("current_price") or {}
            bid = self._safe_float(cp.get("bid"))
            ask = self._safe_float(cp.get("ask"))
            spread = self._safe_float(cp.get("spread"))

            if bid is None or ask is None:
                return None

            if spread is None:
                spread = (ask - bid) / 0.1  # Convert raw price diff to pips (gold pip = 0.1)

            ts = cp.get("timestamp") or dp.get("timestamp") or self._now_iso()
            return {
                "bid": bid,
                "ask": ask,
                "spread": spread,
                "timestamp": ts,
            }
        except Exception:
            return None

    def _log_tool(self, name: str, start_t: float, extra: str = "") -> None:
        try:
            ms = int((time.time() - start_t) * 1000)
            if extra:
                log.info(f"AGENT_TOOL | {name} | {ms}ms | {extra}")
            else:
                log.info(f"AGENT_TOOL | {name} | {ms}ms")
        except Exception:
            pass

    def _log_no_cache(self, name: str, start_t: float, extra: str = "") -> None:
        try:
            msg = "no_cache"
            if extra:
                msg = f"{msg} | {extra}"
            self._log_tool(name, start_t, msg)
        except Exception:
            pass

    def _log_fail(self, name: str, start_t: float, reason: str) -> None:
        try:
            r = str(reason or "").strip()
            self._log_tool(name, start_t, f"fail | {r}" if r else "fail")
        except Exception:
            pass

    # ---------------------------------------------------------------------
    # Market data tools (cache-only)
    # ---------------------------------------------------------------------

    def get_current_price(self) -> Dict[str, Any]:
        start = time.time()
        try:
            dp = self._last_agent_data()
            if not dp:
                self._log_no_cache("get_current_price", start)
                return self._no_cache()

            out = self._extract_price_from_cache(dp)
            if not out:
                self._log_no_cache("get_current_price", start)
                return self._no_cache()

            self._log_tool("get_current_price", start, f"bid={out.get('bid')} ask={out.get('ask')}")
            return out
        except Exception as e:
            self._log_tool("get_current_price", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    def get_position_events(self) -> Dict[str, Any]:
        start = time.time()
        try:
            path = self._agent_monitor_events_path()
            if not os.path.exists(path):
                self._log_tool("get_position_events", start, "empty")
                return {"events": []}

            try:
                import json

                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                self._log_tool("get_position_events", start, "read_failed")
                return {"events": []}

            if not isinstance(data, list):
                data = []

            events = data[-20:]
            self._log_tool("get_position_events", start, f"count={len(events)}")
            return {"events": events}
        except Exception as e:
            self._log_tool("get_position_events", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    def debate_with_rex(
        self,
        my_direction: str,
        my_reasoning: str,
        my_confidence: float,
        key_data: Any,
        rex_previous_response: Any = None,
    ) -> Dict[str, Any]:
        start = time.time()
        try:
            now = time.time()

            # FLO-158: Frequency limit for non-trade decisions (WAIT/HOLD)
            dir_s_check = str(my_direction or "").upper().strip()
            is_trade_decision = any(k in dir_s_check for k in ("BUY", "SELL", "OPEN", "CLOSE", "ADJUST"))
            if not is_trade_decision:
                last_non_trade_rex = getattr(self, "_rex_last_non_trade_ts", 0) or 0
                if (now - float(last_non_trade_rex)) < 3600:
                    elapsed = int(now - float(last_non_trade_rex))
                    self._log_tool("debate_with_rex", start, f"skipped | WAIT/HOLD rate limit ({elapsed}s since last)")
                    return {"success": True, "insights": [], "risk_flags": [], "reason": "rate_limited_non_trade"}
                setattr(self, "_rex_last_non_trade_ts", now)

            last_ts = getattr(self, "_rex_debate_last_ts", None)
            if last_ts is None or (now - float(last_ts)) > 300:
                setattr(self, "_rex_debate_turns", 0)
                setattr(self, "_rex_debate_history", [])
            setattr(self, "_rex_debate_last_ts", now)

            turns = int(getattr(self, "_rex_debate_turns", 0) or 0)
            if turns >= 5:
                return {"success": False, "insights": [], "risk_flags": [], "reason": "debate_turn_limit"}
            turns += 1
            setattr(self, "_rex_debate_turns", turns)

            history = getattr(self, "_rex_debate_history", None)
            if not isinstance(history, list):
                history = []

            dir_s = str(my_direction or "").upper().strip()
            conf_f = self._safe_float(my_confidence)
            if conf_f is None:
                conf_f = 0.0

            dp = self._last_agent_data() or {}
            price = self._extract_price_from_cache(dp) or {}
            mid = None
            try:
                b = self._safe_float(price.get("bid"))
                a = self._safe_float(price.get("ask"))
                if b is not None and a is not None:
                    mid = (b + a) / 2.0
            except Exception:
                mid = None

            indicators = {}
            try:
                indicators = self.get_indicators()
                if not isinstance(indicators, dict):
                    indicators = {}
            except Exception:
                indicators = {}

            ema_blob = self._extract_ema50_ema200(dp)
            if ema_blob.get("ema50") is not None:
                indicators["ema50"] = ema_blob.get("ema50")
            if indicators.get("ema200") is None:
                indicators["ema200"] = ema_blob.get("ema200")

            sr_nearest = []
            try:
                sr = self.get_sr_zones()
                zones = sr.get("zones") if isinstance(sr, dict) else None
                if isinstance(zones, list) and zones:
                    if mid is not None:
                        sr_nearest = self._nearest_sr_zones(zones, mid, limit=5)
                    else:
                        sr_nearest = zones[:5]
            except Exception:
                sr_nearest = []

            fib = {}
            try:
                fib = self.get_fibonacci_levels()
                if not isinstance(fib, dict):
                    fib = {}
            except Exception:
                fib = {}

            macro = {}
            try:
                macro = self.get_macro()
                if not isinstance(macro, dict):
                    macro = {}
            except Exception:
                macro = {}

            headlines = []
            try:
                h = self.get_headlines()
                hh = h.get("headlines") if isinstance(h, dict) else None
                if isinstance(hh, list):
                    headlines = hh[:3]
            except Exception:
                headlines = []

            candles_blob = self._extract_recent_candles_for_rex(dp)

            # FLO-64: Luna brief for independent verification
            luna_context = None
            try:
                from luna_analyst import load_luna_brief
                lb = load_luna_brief()
                if lb and isinstance(lb, dict):
                    luna_context = {
                        "environment": lb.get("environment"),
                        "risk_level": lb.get("risk_level"),
                        "directional_bias": lb.get("directional_bias"),
                        "bias_confidence": lb.get("bias_confidence"),
                        "market_regime": lb.get("market_regime"),
                        "patterns_detected": lb.get("patterns_detected", []),
                        "summary": lb.get("summary", ""),
                    }
            except Exception:
                pass

            # FLO-64: Open position details
            open_position = None
            try:
                positions = getattr(self._bot, "open_positions", None) or {}
                if positions:
                    for _tk, _pos in positions.items():
                        open_position = {
                            "ticket": _tk,
                            "direction": getattr(_pos, "direction", None),
                            "open_price": getattr(_pos, "open_price", None),
                            "sl": getattr(_pos, "sl", None),
                            "tp": getattr(_pos, "tp", None),
                            "volume": getattr(_pos, "volume", None),
                        }
                        break  # First position is enough context
            except Exception:
                pass

            session_name = None
            try:
                session_name = dp.get("session_name")
                if isinstance(session_name, str):
                    session_name = session_name.strip().upper() or None
            except Exception:
                session_name = None
            if session_name is None:
                try:
                    session_name = self._infer_session_from_utc_hour(self._safe_int(dp.get("utc_hour")))
                except Exception:
                    session_name = None

            patterns_context = self._extract_context_for_patterns()
            similar_losses: List[Dict[str, Any]] = []
            try:
                similar_losses = self._query_similar_losing_trades(patterns_context, limit=3)
            except Exception:
                similar_losses = []

            debate_history_lines: List[str] = []
            try:
                for t in history[-5:]:
                    if not isinstance(t, dict):
                        continue
                    tn = t.get("turn")
                    ftxt = str(t.get("floki") or "").strip()
                    rtxt = str(t.get("rex") or "").strip()
                    if ftxt:
                        debate_history_lines.append(f"Turn {tn}: Floki: {ftxt}")
                    if rtxt:
                        debate_history_lines.append(f"Turn {tn}: Rex: {rtxt}")
            except Exception:
                debate_history_lines = []

            payload = {
                "floki": {
                    "direction": dir_s,
                    "confidence": conf_f,
                    "reasoning": str(my_reasoning or "").strip(),
                    "key_data": key_data,
                },
                "rex_previous_response": rex_previous_response,
                "market_context": {
                    "current_price": price or None,
                    "session": self._build_session_context_for_rex(session_name, indicators, dp),
                    "indicators": indicators,
                    "sr_zones_nearest": sr_nearest,
                    "fibonacci": fib,
                    "candles": {
                        "H1_last5": candles_blob.get("H1_last5"),
                        "M5_last3": candles_blob.get("M5_last3"),
                    },
                    "volume_context": candles_blob.get("volume_context"),
                    "macro": macro,
                    "headlines_top3": headlines,
                    "trade_patterns_top3": (patterns_context or {}),
                    "similar_losing_trades_top3": similar_losses,
                    "luna_brief": luna_context,
                    "open_position": open_position,
                },
                "debate_history": "\n".join(debate_history_lines).strip(),
                "turn": turns,
                "turns_max": 5,
            }

            try:
                from rex_validator import validate_with_rex

                rex = validate_with_rex(payload, timeout_seconds=60, agent_tools=self)
            except Exception as e:
                self._log_tool("debate_with_rex", start, f"error={e}")
                return {"success": False, "insights": [], "risk_flags": [], "reason": "rex_unavailable"}

            if not isinstance(rex, dict) or not rex.get("success"):
                _rex_reason = rex.get("reason") if isinstance(rex, dict) else "rex_failed"
                log.warning(f"REX | debate_with_rex failed: {_rex_reason}")
                self._log_tool("debate_with_rex", start, f"failed={_rex_reason}")
                return {"success": False, "insights": [], "risk_flags": [], "reason": _rex_reason}

            # FLO-158: Rex returns insights, not agree/disagree
            insights = rex.get("insights") or []
            risk_flags = rex.get("risk_flags") or []
            # Build summary text from insights + risk_flags
            reasoning_parts = []
            for ins in insights[:3]:
                if isinstance(ins, dict):
                    reasoning_parts.append(f"[{ins.get('type','NOTE')}] {ins.get('observation','')}")
            for flag in risk_flags[:3]:
                reasoning_parts.append(f"[FLAG] {flag}")
            reasoning = "; ".join(reasoning_parts) if reasoning_parts else str(rex.get("raw", ""))[:200]

            try:
                from db_writer import record_agent_event

                floki_text = str(my_reasoning or "").strip()
                if dir_s:
                    floki_text = (f"{dir_s}: " + floki_text).strip()
                if floki_text:
                    record_agent_event(
                        "REX_CONSULT",
                        floki_text[:4000],
                        payload={"turn": turns},
                        author="FLOKI",
                    )

                rex_text = reasoning[:4000]
                if rex_text:
                    record_agent_event(
                        "REX_INSIGHTS",
                        rex_text,
                        payload={"turn": turns, "insights_count": len(insights), "risk_flags": risk_flags},
                        author="REX",
                    )
            except Exception:
                pass

            try:
                history.append(
                    {
                        "turn": turns,
                        "floki": str(my_reasoning or "").strip(),
                        "rex": reasoning,
                        "insights": insights,
                        "risk_flags": risk_flags,
                    }
                )
                setattr(self, "_rex_debate_history", history[-10:])
            except Exception:
                pass

            log.info(
                f"REX_INSIGHTS | turn={turns} | {len(insights)} insights, {len(risk_flags)} flags | {reasoning[:140]}"
            )

            self._log_tool("debate_with_rex", start, f"turn={turns} insights={len(insights)}")
            return {
                "success": True,
                "insights": insights,
                "risk_flags": risk_flags,
                "insights_count": len(insights),
            }
        except Exception as e:
            self._log_tool("debate_with_rex", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    # ---------------------------------------------------------------------
    # Position management tools
    # ---------------------------------------------------------------------

    def set_watch_conditions(self, ticket: int, conditions: List[Dict[str, Any]] = None) -> Dict[str, Any]:
        start = time.time()
        try:
            try:
                t = int(ticket)
            except Exception:
                return {"success": False, "reason": "invalid ticket"}

            if not isinstance(conditions, list) or not conditions:
                self._log_tool("set_watch_conditions", start, f"ticket={t} | missing conditions arg")
                return {
                    "success": False,
                    "reason": "conditions argument required",
                    "hint": "Pass conditions as array of objects, e.g.: conditions=[{type:'pnl_threshold', value:-15}, {type:'price_touch', level:4550}]",
                }

            cleaned: List[Dict[str, Any]] = []
            for c in conditions:
                if not isinstance(c, dict):
                    continue

                ctype = str(c.get("type", "")).strip()
                desc = str(c.get("description", "")).strip()
                if not ctype:
                    continue
                if ctype == "price_touch":
                    lvl = self._safe_float(c.get("level"))
                    if lvl is None:
                        continue
                    cleaned.append({"type": "price_touch", "level": float(lvl), "description": desc})
                elif ctype == "pnl_threshold":
                    v = self._safe_float(c.get("value"))
                    if v is None:
                        continue
                    cleaned.append({"type": "pnl_threshold", "value": float(v), "description": desc})
                elif ctype == "indicator_threshold":
                    ind = str(c.get("indicator", "")).strip().lower()
                    direction = str(c.get("direction", "")).strip().lower()
                    level = self._safe_float(c.get("level"))
                    if ind != "vix" or level is None or direction not in ("above", "below"):
                        continue
                    cleaned.append(
                        {
                            "type": "indicator_threshold",
                            "indicator": "vix",
                            "level": float(level),
                            "direction": direction,
                            "description": desc,
                        }
                    )

            if not cleaned:
                return {"success": False, "reason": "no valid conditions"}

            store = self._load_watch_conditions()
            store[str(t)] = {
                "updated_at": utc_iso(),  # FLO-286
                "conditions": cleaned,
            }

            ok = self._write_json_atomic(self._watch_conditions_path(), store)
            if not ok:
                return {"success": False, "reason": "persist failed"}

            self._log_tool("set_watch_conditions", start, f"ticket={t} count={len(cleaned)}")
            return {"success": True, "ticket": t, "count": len(cleaned)}
        except Exception as e:
            self._log_tool("set_watch_conditions", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    def set_wake_conditions(self, max_sleep_minutes: int, conditions: List[Dict[str, Any]]) -> Dict[str, Any]:
        start = time.time()
        try:
            try:
                msm = int(max_sleep_minutes)
            except Exception:
                return {"success": False, "reason": "invalid max_sleep_minutes"}

            if msm <= 0:
                return {"success": False, "reason": "max_sleep_minutes must be positive"}

            if not isinstance(conditions, list) or not conditions:
                return {"success": False, "reason": "conditions must be a non-empty list"}

            allowed_types = {
                "price_above",
                "price_below",
                "price_touch",
                "h1_volume_above",
                "scanner_pattern",
                "indicator_above",
                "indicator_below",
            }

            cleaned: List[Dict[str, Any]] = []
            for idx, c in enumerate(conditions, start=1):
                if not isinstance(c, dict):
                    continue

                ctype = str(c.get("type", "")).strip()
                if not ctype or ctype not in allowed_types:
                    continue

                desc = str(c.get("description", "")).strip()
                cid = str(c.get("id") or "").strip() or f"c{idx}"

                if ctype in ("price_above", "price_below", "price_touch"):
                    lvl = self._safe_float(c.get("level"))
                    if lvl is None:
                        continue
                    cleaned.append({"id": cid, "type": ctype, "level": float(lvl), "description": desc})
                elif ctype == "h1_volume_above":
                    thr = self._safe_float(c.get("threshold"))
                    if thr is None:
                        continue
                    cleaned.append({"id": cid, "type": ctype, "threshold": float(thr), "description": desc})
                elif ctype == "scanner_pattern":
                    pat = str(c.get("pattern") or "").strip()
                    if not pat:
                        continue
                    cleaned.append({"id": cid, "type": ctype, "pattern": pat, "description": desc})
                elif ctype in ("indicator_above", "indicator_below"):
                    ind = str(c.get("indicator") or "").strip().lower()
                    thr = self._safe_float(c.get("threshold"))
                    if not ind or thr is None:
                        continue
                    cleaned.append({"id": cid, "type": ctype, "indicator": ind, "threshold": float(thr), "description": desc})

            if not cleaned:
                return {"success": False, "reason": "no valid conditions"}

            # FLO-204: Preserve fired_ids for conditions with same ID AND same value.
            # If Floki re-sets the same conditions after being woken, already-fired
            # conditions stay fired. If Floki changes a value (e.g., price_below from
            # 4654 to 4622), the condition is treated as new and will trigger.
            preserved_fired = []
            try:
                wc_path = self._wake_conditions_path()
                if os.path.exists(wc_path):
                    with open(wc_path, "r", encoding="utf-8") as f:
                        old_wc = json.loads(f.read())
                    old_fired = set(str(x) for x in (old_wc.get("fired_ids") or []) if x)
                    if old_fired:
                        # Build lookup: id → signature (type + level/threshold/pattern)
                        old_conds = {str(c.get("id")): c for c in (old_wc.get("conditions") or []) if isinstance(c, dict)}
                        for nc in cleaned:
                            nid = str(nc.get("id"))
                            if nid not in old_fired:
                                continue
                            oc = old_conds.get(nid)
                            if not oc:
                                continue
                            # Same ID — check if value also matches
                            same = (nc.get("type") == oc.get("type")
                                    and nc.get("level") == oc.get("level")
                                    and nc.get("threshold") == oc.get("threshold")
                                    and nc.get("pattern") == oc.get("pattern"))
                            if same:
                                preserved_fired.append(nid)
            except Exception:
                preserved_fired = []

            now_iso = utc_iso()  # FLO-286
            payload = {
                "updated_at": now_iso,
                "sleep_started_at": now_iso,
                "max_sleep_minutes": msm,
                "conditions": cleaned,
            }
            if preserved_fired:
                payload["fired_ids"] = preserved_fired

            ok = self._write_json_atomic(self._wake_conditions_path(), payload)
            if not ok:
                return {"success": False, "reason": "persist failed"}

            # Sync price-level conditions to EA for tick-level monitoring + chart lines
            try:
                import config as _cfg_ea
                _fired_set = set(str(x) for x in (payload.get("fired_ids") or []))
                _ea_alerts = []
                for c in cleaned:
                    if c.get("type") in ("price_above", "price_below", "price_touch") and c.get("level") is not None:
                        if str(c.get("id", "")) not in _fired_set:
                            _ea_alerts.append({"id": str(c["id"]), "type": c["type"], "level": float(c["level"])})
                _ea_payload = {
                    "version": 1,
                    "timestamp": utc_iso(),  # FLO-309
                    "alerts": _ea_alerts,
                }
                _ea_path = _cfg_ea.PRICE_ALERTS_JSON_PATH
                _ea_tmp = _ea_path + ".tmp"
                with open(_ea_tmp, "w", encoding="utf-8") as f:
                    json.dump(_ea_payload, f, ensure_ascii=False, indent=2)
                os.replace(_ea_tmp, _ea_path)
            except Exception:
                pass

            try:
                from db_writer import record_agent_event

                def _fmt_minutes(m: int) -> str:
                    try:
                        m_i = int(m)
                    except Exception:
                        m_i = 0
                    if m_i <= 0:
                        return "0 minutes"
                    if m_i % 60 == 0:
                        h = int(m_i / 60)
                        return f"{h} hour" if h == 1 else f"{h} hours"
                    return f"{m_i} minutes"

                parts = []
                for c in cleaned[:6]:
                    try:
                        ctype = str(c.get("type") or "").strip()
                        desc_s = str(c.get("description") or "").strip()
                        if ctype in ("price_above", "price_below"):
                            lvl = c.get("level")
                            direction = "above" if ctype == "price_above" else "below"
                            seg = f"price {direction} {lvl}"
                            if desc_s:
                                seg += f" ({desc_s})"
                            parts.append(seg)
                        elif ctype == "h1_volume_above":
                            thr = c.get("threshold")
                            seg = f"H1 volume above {thr}"
                            if desc_s:
                                seg += f" ({desc_s})"
                            parts.append(seg)
                        elif ctype == "scanner_pattern":
                            pat = c.get("pattern")
                            seg = f"pattern {pat}"
                            if desc_s:
                                seg += f" ({desc_s})"
                            parts.append(seg)
                        elif ctype in ("indicator_above", "indicator_below"):
                            ind = c.get("indicator")
                            thr = c.get("threshold")
                            direction = "above" if ctype == "indicator_above" else "below"
                            seg = f"{ind} {direction} {thr}"
                            if desc_s:
                                seg += f" ({desc_s})"
                            parts.append(seg)
                    except Exception:
                        continue

                monitoring = " and ".join(parts) if parts else f"{len(cleaned)} condition(s)"
                content = (
                    f"Got it boss. Monitoring: {monitoring}. "
                    f"Max sleep: {_fmt_minutes(msm)}."
                )
                record_agent_event("SIMBA_ACK", content, payload=payload, author="SIMBA")
            except Exception:
                pass

            self._log_tool("set_wake_conditions", start, f"count={len(cleaned)} max_sleep_minutes={msm}")
            return {"success": True, "count": len(cleaned), "max_sleep_minutes": msm}
        except Exception as e:
            self._log_tool("set_wake_conditions", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    def get_candles(self, timeframe: str, count: int) -> Dict[str, Any]:
        start = time.time()
        try:
            tf = str(timeframe or "").upper().strip()
            _TF_ALIASES = {"4H": "H4", "1H": "H1", "1D": "D1", "15M": "M15", "5M": "M5", "30M": "M30"}
            tf = _TF_ALIASES.get(tf, tf)
            if tf not in ("M5", "M15", "M30", "H1", "H4", "D1"):
                self._log_fail("get_candles", start, "unsupported timeframe")
                return {"success": False, "reason": f"unsupported timeframe '{tf}'. Use: M5, M15, H1, H4, D1"}

            try:
                c = int(count)
            except Exception:
                c = 0
            if c <= 0:
                self._log_fail("get_candles", start, "count must be positive")
                return {"success": False, "reason": "count must be positive"}
            # FLO-166: H1 default 50 candles (2 days) for proper swing visibility
            if tf == "H1":
                c = max(c, 50)
            c = min(c, 100)

            dp = self._last_agent_data() or {}

            candles: Optional[List[Dict[str, Any]]] = None

            # Preferred cache source: data_package has a candles section (if present)
            try:
                cds = dp.get("candles") or {}
                if isinstance(cds, dict):
                    maybe = cds.get(tf)
                    if isinstance(maybe, list) and maybe:
                        candles = maybe
            except Exception:
                candles = None

            # H1 fallback: DataFrame cache
            if candles is None and tf == "H1":
                df = self._last_df()
                if df is None:
                    self._log_no_cache("get_candles", start, f"{tf} x {c}")
                    return self._no_cache()
                try:
                    # Expect columns: time, open, high, low, close, tick_volume/volume
                    cols = set(getattr(df, "columns", []))
                    required = {"open", "high", "low", "close"}
                    if not required.issubset(cols):
                        self._log_fail("get_candles", start, "missing cached df columns")
                        return {"success": False, "reason": "missing cached df columns"}

                    tail = df.tail(c)
                    out_list: List[Dict[str, Any]] = []
                    for _, row in tail.iterrows():
                        t = None
                        if "time" in cols:
                            try:
                                t = row["time"]
                                if hasattr(t, "isoformat"):
                                    t = t.isoformat()
                                else:
                                    t = str(t)
                            except Exception:
                                t = None

                        vol = None
                        if "tick_volume" in cols:
                            vol = row.get("tick_volume")
                        elif "volume" in cols:
                            vol = row.get("volume")

                        out_list.append(
                            {
                                "time": t,
                                "open": float(row["open"]),
                                "high": float(row["high"]),
                                "low": float(row["low"]),
                                "close": float(row["close"]),
                                "volume": float(vol) if vol is not None else 0.0,
                            }
                        )
                    candles = out_list
                except Exception:
                    self._log_fail("get_candles", start, "failed to build candles from cache")
                    return {"success": False, "reason": "failed to build candles from cache"}

            if candles is None:
                self._log_no_cache("get_candles", start, f"{tf} x {c}")
                return self._no_cache()

            candles = candles[-c:]

            # FLO-225: Enrich candles with indicator values per bar
            # Gives Floki indicator history — RSI divergences, BB squeezes, MACD patterns
            try:
                import pandas as pd
                import math
                from technical_analyzer import calculate_indicators

                def _rn(v, d):
                    """Round or None for NaN/missing values."""
                    if v is None:
                        return None
                    try:
                        f = float(v)
                        if math.isnan(f):
                            return None
                        return round(f, d)
                    except Exception:
                        return None

                _edf = pd.DataFrame(candles)
                if len(_edf) >= 14 and {"open", "high", "low", "close"}.issubset(_edf.columns):
                    _edf = calculate_indicators(_edf)
                    for _ei in range(len(candles)):
                        _er = _edf.iloc[_ei]
                        candles[_ei]["rsi"] = _rn(_er.get("rsi_14"), 1)
                        candles[_ei]["macd"] = _rn(_er.get("macd"), 2)
                        candles[_ei]["macd_signal"] = _rn(_er.get("macd_signal"), 2)
                        candles[_ei]["macd_hist"] = _rn(_er.get("macd_hist"), 2)
                        _bbu = _rn(_er.get("bb_upper"), 2)
                        _bbl = _rn(_er.get("bb_lower"), 2)
                        candles[_ei]["bb_upper"] = _bbu
                        candles[_ei]["bb_lower"] = _bbl
                        candles[_ei]["bb_mid"] = _rn(_er.get("bb_middle"), 2)
                        candles[_ei]["bb_width"] = round(_bbu - _bbl, 2) if _bbu is not None and _bbl is not None else None
                        candles[_ei]["ema9"] = _rn(_er.get("ema_9"), 2)
                        candles[_ei]["ema21"] = _rn(_er.get("ema_21"), 2)
                        candles[_ei]["ema50"] = _rn(_er.get("ema_50"), 2)
                        candles[_ei]["ema200"] = _rn(_er.get("ema_200"), 2)
            except Exception:
                pass  # Enrichment failure is non-fatal — return plain candles

            self._log_tool("get_candles", start, f"{tf} x {len(candles)}")
            return {"timeframe": tf, "candles": candles}
        except Exception as e:
            self._log_tool("get_candles", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    def get_indicators(self) -> Dict[str, Any]:
        start = time.time()
        try:
            dp = self._last_agent_data()
            if not dp:
                self._log_no_cache("get_indicators", start)
                return self._no_cache()

            ind = dp.get("indicators")
            if not isinstance(ind, dict) or not ind:
                self._log_no_cache("get_indicators", start)
                return self._no_cache()

            # Return a simplified, model-friendly view while preserving numeric values.
            out: Dict[str, Any] = {}

            try:
                rsi = ind.get("rsi") or {}
                out["rsi"] = self._safe_float(rsi.get("value"))
            except Exception:
                out["rsi"] = None

            try:
                macd = ind.get("macd") or {}
                out["macd"] = {
                    "value": self._safe_float(macd.get("value")),
                    "signal": self._safe_float(macd.get("signal")),
                    "histogram": self._safe_float(macd.get("histogram")),
                }
            except Exception:
                out["macd"] = {"value": None, "signal": None, "histogram": None}

            try:
                emas = ind.get("emas") or {}
                out["ema50"] = self._safe_float(emas.get("ema50"))
                out["ema200"] = self._safe_float(emas.get("ema200"))
            except Exception:
                out["ema50"] = None
                out["ema200"] = None

            try:
                atr = ind.get("atr") or {}
                out["atr"] = self._safe_float(atr.get("value"))
            except Exception:
                out["atr"] = None

            try:
                adx = ind.get("adx") or {}
                out["adx"] = {
                    "value": self._safe_float(adx.get("value")),
                    "plus_di": self._safe_float(adx.get("plus_di")),
                    "minus_di": self._safe_float(adx.get("minus_di")),
                }
            except Exception:
                out["adx"] = {"value": None, "plus_di": None, "minus_di": None}

            try:
                bb = ind.get("bollinger") or {}
                out["bollinger"] = {
                    "upper": self._safe_float(bb.get("upper")),
                    "middle": self._safe_float(bb.get("middle")),
                    "lower": self._safe_float(bb.get("lower")),
                    "position_pct": self._safe_float(bb.get("position_pct")),
                }
            except Exception:
                out["bollinger"] = {"upper": None, "middle": None, "lower": None, "position_pct": None}

            # FLO-164 Fix 1: 5-bar trend enrichment from H1 candle history
            try:
                import numpy as np
                dp_candles = dp.get("candles", {})
                h1_raw = dp_candles.get("H1") if isinstance(dp_candles, dict) else None
                if isinstance(h1_raw, list) and len(h1_raw) >= 6:
                    closes = [float(c.get("close", c[4]) if isinstance(c, dict) else c[4]) for c in h1_raw[-20:]]
                    highs = [float(c.get("high", c[2]) if isinstance(c, dict) else c[2]) for c in h1_raw[-20:]]
                    lows = [float(c.get("low", c[3]) if isinstance(c, dict) else c[3]) for c in h1_raw[-20:]]

                    def _rsi(data, period=14):
                        if len(data) < period + 1:
                            return None
                        deltas = np.diff(data)
                        gains = np.where(deltas > 0, deltas, 0)
                        losses = np.where(deltas < 0, -deltas, 0)
                        ag = np.mean(gains[-period:])
                        al = np.mean(losses[-period:])
                        if al == 0:
                            return 100.0
                        return round(100 - (100 / (1 + ag / al)), 1)

                    rsi_now = _rsi(closes)
                    rsi_5ago = _rsi(closes[:-5]) if len(closes) > 19 else None

                    if rsi_now is not None and rsi_5ago is not None:
                        out["rsi_5bar_ago"] = rsi_5ago
                        diff = rsi_now - rsi_5ago
                        out["rsi_direction"] = "rising" if diff > 3 else ("falling" if diff < -3 else "flat")

                    # MACD histogram trend
                    if len(closes) >= 26:
                        def _macd_hist(data):
                            ema12 = [data[0]]
                            ema26 = [data[0]]
                            m12, m26 = 2.0/13.0, 2.0/27.0
                            for c in data[1:]:
                                ema12.append(c * m12 + ema12[-1] * (1 - m12))
                                ema26.append(c * m26 + ema26[-1] * (1 - m26))
                            macd_line = [e12 - e26 for e12, e26 in zip(ema12, ema26)]
                            signal = [macd_line[0]]
                            m9 = 2.0/10.0
                            for v in macd_line[1:]:
                                signal.append(v * m9 + signal[-1] * (1 - m9))
                            return macd_line[-1] - signal[-1]

                        hist_now = _macd_hist(closes)
                        hist_5ago = _macd_hist(closes[:-5])
                        diff_h = hist_now - hist_5ago
                        out["macd_histogram_direction"] = "rising" if diff_h > 0.5 else ("falling" if diff_h < -0.5 else "flat")

                    # ADX direction from actual 4-bar change (FLO-240: no threshold bias)
                    adx_change = self._safe_float(out.get("adx_change_4bars"))
                    if adx_change is not None:
                        out["adx_direction"] = "rising" if adx_change > 2 else ("falling" if adx_change < -2 else "steady")

                    # Bollinger width + direction
                    bb_u = self._safe_float((ind.get("bollinger") or {}).get("upper"))
                    bb_l = self._safe_float((ind.get("bollinger") or {}).get("lower"))
                    bb_m = self._safe_float((ind.get("bollinger") or {}).get("middle"))
                    if bb_u and bb_l and bb_m and bb_m > 0:
                        width_now = (bb_u - bb_l) / bb_m * 100
                        out["bb_width_pct"] = round(width_now, 2)
                        # Estimate 5-bar-ago width from candle range
                        if len(highs) >= 20 and len(lows) >= 20:
                            avg_range_recent = np.mean([h - l for h, l in zip(highs[-5:], lows[-5:])])
                            avg_range_prior = np.mean([h - l for h, l in zip(highs[-10:-5], lows[-10:-5])])
                            if avg_range_prior > 0:
                                out["bb_width_direction"] = "expanding" if avg_range_recent > avg_range_prior * 1.15 else ("squeezing" if avg_range_recent < avg_range_prior * 0.85 else "stable")
            except Exception:
                pass

            # FLO-221: Append multi-TF indicators (M15, H1, H4, D1)
            try:
                mtf = dp.get("multi_tf_indicators")
                if isinstance(mtf, dict) and mtf:
                    for tf_key in ["M15", "H1", "H4", "D1"]:
                        if tf_key in mtf:
                            out[tf_key] = mtf[tf_key]
            except Exception:
                pass

            self._log_tool("get_indicators", start)
            return out
        except Exception as e:
            self._log_tool("get_indicators", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    def get_sr_zones(self, timeframe: str = "") -> Dict[str, Any]:
        start = time.time()
        try:
            # FLO-262: If timeframe specified, use per-TF zones
            tf = (timeframe or "").strip().upper()
            if tf and tf in ("D1", "H4", "H1"):
                per_tf = getattr(self._bot, '_last_sr_zones_per_tf', None)
                if per_tf and isinstance(per_tf, dict) and tf in per_tf:
                    tf_zones = per_tf[tf]
                    zones = []
                    for z in tf_zones:
                        zones.append({
                            "price": round(z.midpoint, 2),
                            "zone_type": z.zone_type,
                            "touches": z.touches,
                            "timeframe": z.timeframe,
                            "confluence": z.confluence if z.confluence else [],
                            "strength": z.strength,
                            "is_confluence": len(z.confluence) > 1,
                            "volume": int(getattr(z, "volume", 0)),            # FLO-312
                            "volume_bucket": getattr(z, "volume_bucket", "—"),   # FLO-312
                        })
                    _h = sum(1 for z in zones if z.get("volume_bucket") == "HIGH")
                    _l = sum(1 for z in zones if z.get("volume_bucket") == "LOW")
                    self._log_tool("get_sr_zones", start,
                                   f"tf={tf} zones={len(zones)} vol_H/L={_h}/{_l}")
                    # Fall through to enrichment below
                    # (skip the merged-zones path)
                else:
                    self._log_tool("get_sr_zones", start, f"tf={tf} per-TF data not available, using merged")
                    tf = ""  # fall through to merged path

            if not tf:
                dp = self._last_agent_data()
                if not dp:
                    self._log_no_cache("get_sr_zones", start)
                    return self._no_cache()

                sr = dp.get("sr_zones") or dp.get("support_resistance")
                if isinstance(sr, dict) and "zones" in sr:
                    zones = sr.get("zones")
                else:
                    zones = sr

            if not isinstance(zones, list) or not zones:
                self._log_no_cache("get_sr_zones", start)
                return self._no_cache()

            # FLO-111: Filter to 8 most relevant zones (4 above + 4 below price)
            raw_count = len(zones)
            try:
                cp = dp.get("current_price") or {}
                price = self._safe_float(cp.get("mid")) or self._safe_float(cp.get("bid"))
                if price:
                    above = []
                    below = []
                    for z in zones:
                        zp = z.get("price") if isinstance(z, dict) else getattr(z, "midpoint", None)
                        if zp is None:
                            zp = z.get("midpoint", 0) if isinstance(z, dict) else 0
                        if zp > price:
                            above.append(z)
                        else:
                            below.append(z)
                    above.sort(key=lambda z: abs((z.get("price") or z.get("midpoint", 0)) - price))
                    below.sort(key=lambda z: abs((z.get("price") or z.get("midpoint", 0)) - price))
                    zones = above[:4] + below[:4]
            except Exception:
                pass

            # FLO-240: Cross-reference with pivot points, confluence zones first
            try:
                _bs_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "bot_state.json")
                with open(_bs_path, "r", encoding="utf-8") as _bsf:
                    _bs_sr = json.load(_bsf)
                _pivots = {}
                for _layer in ("daily", "weekly", "monthly"):
                    _cl = _bs_sr.get("pivot_points", {}).get(_layer, {}).get("classic", {})
                    for _pk, _pv in _cl.items():
                        if _pv:
                            _pivots[f"{_layer}_{_pk}"] = float(_pv)
                _with_conf = []
                _without_conf = []
                for z in zones:
                    zp = float(z.get("price") or z.get("midpoint", 0) or 0)
                    if not zp:
                        _without_conf.append(z)
                        continue
                    confl = []
                    for _pk, _pv in _pivots.items():
                        if abs(zp - _pv) < 10:
                            confl.append(f"{_pk} ({abs(zp - _pv):.1f})")
                    if confl:
                        z["pivot_confluence"] = confl
                        _with_conf.append(z)
                    else:
                        _without_conf.append(z)
                if len(_with_conf) >= 3:
                    zones = _with_conf
                else:
                    zones = _with_conf + _without_conf
            except Exception:
                pass

            # FLO-244: Label zones with role + direction-aware test_type for nearby levels
            try:
                _cp_role = self._safe_float((dp.get("current_price") or {}).get("mid")) or self._safe_float((dp.get("current_price") or {}).get("bid"))
                if _cp_role:
                    # Determine price direction from bot_state
                    _price_dir = "FLAT"
                    try:
                        _bs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "bot_state.json")
                        with open(_bs_dir, "r", encoding="utf-8") as _fd:
                            _pct = json.load(_fd).get("price_daily_change_pct", 0) or 0
                        _price_dir = "FALLING" if _pct < -0.1 else ("RISING" if _pct > 0.1 else "FLAT")
                    except Exception:
                        pass

                    for z in zones:
                        _zp = float(z.get("price") or z.get("midpoint", 0) or 0)
                        _zt = str(z.get("zone_type", "")).upper()
                        _dist = round(abs(_cp_role - _zp), 1)

                        # Base role from position — overwrite zone_type so Floki says SUPPORT/RESISTANCE not FLIP
                        if _zp < _cp_role:
                            z["role"] = "SUPPORT"
                            if _zt == "FLIP":
                                z["flip_phase"] = "resistance \u2192 support"
                            z["zone_type"] = "SUPPORT"
                        elif _zp > _cp_role:
                            z["role"] = "RESISTANCE"
                            if _zt == "FLIP":
                                z["flip_phase"] = "support \u2192 resistance"
                            z["zone_type"] = "RESISTANCE"
                        else:
                            z["role"] = "AT_PRICE"
                            z["zone_type"] = "AT_PRICE"
                        z["distance_pips"] = _dist

                        # Direction-aware test type for nearby zones (<5 pips)
                        if _dist < 5:
                            if _price_dir == "FALLING":
                                z["test_type"] = "SUPPORT_TEST"
                                z["test_note"] = f"Price falling toward {_zp:.1f} \u2014 testing as support"
                            elif _price_dir == "RISING":
                                z["test_type"] = "RESISTANCE_TEST"
                                z["test_note"] = f"Price rising toward {_zp:.1f} \u2014 testing as resistance"
                            else:
                                z["test_type"] = "CONSOLIDATING"
                                z["test_note"] = f"Price flat near {_zp:.1f} \u2014 consolidating at level"
            except Exception:
                pass

            self._log_tool("get_sr_zones", start, f"zones={len(zones)} (raw={raw_count})")
            return {"zones": zones}
        except Exception as e:
            self._log_tool("get_sr_zones", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    def get_fibonacci_levels(self) -> Dict[str, Any]:
        start = time.time()
        try:
            dp = self._last_agent_data()
            if not dp:
                self._log_no_cache("get_fibonacci_levels", start)
                return self._no_cache()

            fib = dp.get("fibonacci") or dp.get("fib")
            if not isinstance(fib, dict) or not fib:
                self._log_no_cache("get_fibonacci_levels", start)
                return self._no_cache()

            # Multi-timeframe structure expected:
            # {"H1": {"swing_high":..., "swing_low":..., "levels": {...}}, "H4": {...}, "D1": {...}}
            # Only available timeframes are included.
            if any(tf in fib for tf in ("H1", "H4", "D1")):
                out = {}
                for tf in ("H1", "H4", "D1"):
                    v = fib.get(tf)
                    if not isinstance(v, dict) or not v:
                        continue
                    levels = v.get("levels")
                    if not isinstance(levels, dict) or not levels:
                        continue
                    out[tf] = {
                        "levels": levels,
                        "swing_high": v.get("swing_high"),
                        "swing_low": v.get("swing_low"),
                    }
                if not out:
                    self._log_no_cache("get_fibonacci_levels", start)
                    return self._no_cache()
                self._log_tool("get_fibonacci_levels", start, f"tfs={','.join(out.keys())}")
                return out

            # Backward-compatible: single timeframe structure
            levels = fib.get("levels") if isinstance(fib.get("levels"), dict) else fib.get("levels")
            if not isinstance(levels, dict) or not levels:
                self._log_no_cache("get_fibonacci_levels", start)
                return self._no_cache()

            out = {
                "H1": {
                    "levels": levels,
                    "swing_high": fib.get("swing_high"),
                    "swing_low": fib.get("swing_low"),
                }
            }
            self._log_tool("get_fibonacci_levels", start, "tfs=H1")
            return out
        except Exception as e:
            self._log_tool("get_fibonacci_levels", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    def get_pivot_points(self) -> Dict[str, Any]:
        """FLO-223: Return Classic + Fibonacci Pivot Points from previous D1 candle."""
        start = time.time()
        try:
            dp = self._last_agent_data()
            if not dp:
                self._log_no_cache("get_pivot_points", start)
                return self._no_cache()

            pp = dp.get("pivot_points")
            if not isinstance(pp, dict) or not pp:
                self._log_no_cache("get_pivot_points", start)
                return self._no_cache()

            self._log_tool("get_pivot_points", start, f"PP={pp.get('classic', {}).get('PP', '?')}")
            return pp
        except Exception as e:
            self._log_tool("get_pivot_points", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    # ---------------------------------------------------------------------
    # Context tools (cache-only)
    # ---------------------------------------------------------------------

    def get_headlines(self) -> Dict[str, Any]:
        start = time.time()
        try:
            dp = self._last_agent_data()
            if not dp:
                self._log_no_cache("get_headlines", start)
                return self._no_cache()

            news = dp.get("headlines") or dp.get("news") or dp.get("news_headlines")
            if isinstance(news, dict) and "headlines" in news:
                headlines = news.get("headlines")
            else:
                headlines = news

            if not isinstance(headlines, list):
                self._log_no_cache("get_headlines", start)
                return self._no_cache()

            out = {"headlines": headlines[:10], "count": min(len(headlines), 10)}
            self._log_tool("get_headlines", start, f"count={out['count']}")
            return out
        except Exception as e:
            self._log_tool("get_headlines", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    def get_macro(self) -> Dict[str, Any]:
        start = time.time()
        try:
            dp = self._last_agent_data()
            if not dp:
                self._log_no_cache("get_macro", start)
                return self._no_cache()

            macro = dp.get("macro")
            if not isinstance(macro, dict) or not macro:
                self._log_no_cache("get_macro", start)
                return self._no_cache()

            self._log_tool("get_macro", start)
            return macro
        except Exception as e:
            self._log_tool("get_macro", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    def get_calendar(self) -> Dict[str, Any]:
        start = time.time()
        try:
            dp = self._last_agent_data()
            if not dp:
                self._log_no_cache("get_calendar", start)
                return self._no_cache()

            cal = dp.get("calendar") or dp.get("economic_calendar")
            if not isinstance(cal, dict) or not cal:
                self._log_no_cache("get_calendar", start)
                return self._no_cache()

            self._log_tool("get_calendar", start)
            return cal
        except Exception as e:
            self._log_tool("get_calendar", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    def get_ml_prediction(self) -> Dict[str, Any]:
        start = time.time()
        try:
            dp = self._last_agent_data()
            if not dp:
                self._log_no_cache("get_ml_prediction", start)
                return self._no_cache()

            ml = dp.get("ml") or dp.get("ml_prediction") or dp.get("ml_predictions")
            if not isinstance(ml, dict) or not ml:
                self._log_no_cache("get_ml_prediction", start)
                return self._no_cache()

            self._log_tool("get_ml_prediction", start)
            return ml
        except Exception as e:
            self._log_tool("get_ml_prediction", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    # ---------------------------------------------------------------------
    # Market context (correlated instruments from MT5)
    # ---------------------------------------------------------------------

    def get_market_context(self) -> Dict[str, Any]:
        """Read correlated MT5 instruments for broader market picture."""
        start = time.time()
        try:
            from market_context_fetcher import fetch_market_context

            result = fetch_market_context()
            if not result:
                self._log_tool("get_market_context", start, "no_data")
                return {"success": False, "reason": "no_data"}

            # Enrich with volume ratio from agent data (not available in fetcher)
            try:
                dp = self._last_agent_data()
                vol = (dp.get("indicators") or {}).get("volume") if dp else None
                if isinstance(result.get("session"), dict) and isinstance(vol, dict):
                    result["session"]["volume_ratio"] = vol.get("tick_volume_ratio")
            except Exception:
                pass

            n_live = sum(1 for cat in result.values() if isinstance(cat, dict) for v in cat.values() if isinstance(v, dict) and v.get("bid"))
            self._log_tool("get_market_context", start, f"live={n_live}")
            return result
        except Exception as e:
            self._log_tool("get_market_context", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    # ---------------------------------------------------------------------
    # Portfolio tools (execution layer is allowed)
    # ---------------------------------------------------------------------

    def get_open_positions(self) -> Dict[str, Any]:
        start = time.time()
        try:
            positions = []
            try:
                positions = self._executor.get_open_positions() or []
            except Exception:
                positions = []

            out_positions = []
            for p in positions:
                try:
                    out_positions.append(
                        {
                            "ticket": int(getattr(p, "ticket", 0)),
                            "direction": str(getattr(p, "direction", "")),
                            "entry": float(getattr(p, "open_price", 0.0)),
                            "sl": float(getattr(p, "sl", 0.0)),
                            "tp": float(getattr(p, "tp", 0.0)),
                            "current_pnl": float(getattr(p, "profit", 0.0)),
                            "phase": "OPEN",
                        }
                    )
                except Exception:
                    continue

            self._log_tool("get_open_positions", start, f"count={len(out_positions)}")
            return {"positions": out_positions, "count": len(out_positions)}
        except Exception as e:
            self._log_tool("get_open_positions", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    def get_account_info(self) -> Dict[str, Any]:
        start = time.time()
        try:
            info = self._executor.get_account_info()
            if not isinstance(info, dict) or not info:
                return {"success": False, "reason": "account info unavailable"}

            out = {
                "balance": self._safe_float(info.get("balance")),
                "equity": self._safe_float(info.get("equity")),
                "margin_used": self._safe_float(info.get("margin")),
                "leverage": info.get("leverage"),
            }
            self._log_tool("get_account_info", start)
            return out
        except Exception as e:
            self._log_tool("get_account_info", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    def get_trade_history(self, days: int) -> Dict[str, Any]:
        start = time.time()
        try:
            try:
                d = int(days)
            except Exception:
                d = 1
            d = max(1, min(d, 30))

            # Prefer db_writer if available
            trades = []
            summary = {"wins": 0, "losses": 0, "pnl": 0.0}
            try:
                from db_writer import get_recent_agent_decisions  # noqa: F401
                # No dedicated helper found here for trade history; fall back to MT5 deal history helper if exposed.
            except Exception:
                pass

            # Use existing executor helper if available
            try:
                from executor import get_recent_closed_deals

                deals = get_recent_closed_deals(hours=d * 24) or []
                for deal in deals:
                    try:
                        profit = float(deal.get("profit", 0.0) or 0.0)
                        trades.append(
                            {
                                "ticket": int(deal.get("position_id", 0) or 0),
                                "direction": deal.get("direction"),
                                "profit": profit,
                                "close_reason": deal.get("reason"),
                                "close_time": str(deal.get("close_time")),
                            }
                        )
                        summary["pnl"] += profit
                        if profit > 0:
                            summary["wins"] += 1
                        elif profit < 0:
                            summary["losses"] += 1
                    except Exception:
                        continue
            except Exception:
                # No history available is not fatal
                pass

            self._log_tool("get_trade_history", start, f"days={d} trades={len(trades)}")
            return {"trades": trades, "summary": summary}
        except Exception as e:
            self._log_tool("get_trade_history", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    # ---------------------------------------------------------------------
    # Action tools (execution allowed; safety enforced here)
    # ---------------------------------------------------------------------

    def execute_trade(
        self,
        direction: str,
        sl: float,
        tp: float,
        agent_confidence: Optional[float] = None,
    ) -> Dict[str, Any]:
        start = time.time()
        try:
            dp = self._last_agent_data()
            if not dp:
                return self._no_cache()

            price = self._extract_price_from_cache(dp)
            if not price:
                return self._no_cache()

            try:
                import config

                max_spread = float(getattr(config, "MAX_SPREAD_PIPS", 5.0))
            except Exception:
                max_spread = 5.0

            try:
                spread_pips = float(price.get("spread") or 0.0)
                if spread_pips > max_spread:
                    self._log_tool("execute_trade", start, f"{str(direction).upper()} | REJECTED | spread {spread_pips:.1f} > max {max_spread:.1f}")
                    return {
                        "success": False,
                        "reason": f"spread too high: {spread_pips:.1f} pips > max {max_spread:.1f} pips",
                    }
            except Exception:
                pass

            m5_warning = None

            # M5 reversal check (warn only; never block)
            try:
                from momentum_detector import check_m5_reversal

                m5_check = check_m5_reversal(str(direction or ""))
                if isinstance(m5_check, dict) and m5_check.get("reversal_detected"):
                    strength = str(m5_check.get("reversal_strength") or "").lower()
                    if strength == "strong":
                        m5_warning = "M5 ALERT: strong counter-movement detected"
                        desc = str(m5_check.get("description") or "M5 reversal")
                        log.warning(f"AGENT_TOOL | {m5_warning} | {desc}")
                    elif strength == "moderate":
                        m5_warning = "M5 NOTE: moderate counter-movement"
                        desc = str(m5_check.get("description") or "M5 reversal")
                        log.warning(f"AGENT_TOOL | {m5_warning} | {desc}")
            except Exception:
                # Fail-open: reversal check must never block execution due to tool errors
                pass

            dir_s = str(direction or "").upper().strip()
            if dir_s not in ("BUY", "SELL"):
                return {"success": False, "reason": "invalid direction"}

            sl_f = self._safe_float(sl)
            tp_f = self._safe_float(tp)
            if sl_f is None or tp_f is None:
                return {"success": False, "reason": "invalid sl/tp"}

            # Compute entry reference from cached bid/ask (analysis price). Execution will get real tick.
            entry_ref = float(price["ask"] if dir_s == "BUY" else price["bid"])

            sl_pips = self._sl_pips_from_prices(entry_ref, sl_f)
            if sl_pips is None:
                self._log_tool("execute_trade", start, f"{dir_s} | REJECTED | could not compute sl pips")
                return {"success": False, "reason": "could not compute sl pips"}

            # Safety checks (market open, MT5 connected, opposing positions)
            acct = self._executor.get_account_info() or {}
            balance = self._safe_float(acct.get("balance"))
            if balance is None:
                self._log_tool("execute_trade", start, f"{dir_s} | REJECTED | account balance unavailable")
                return {"success": False, "reason": "account balance unavailable"}

            open_positions_list = None
            try:
                open_positions_list = self._executor.get_open_positions() or []
            except Exception:
                log.warning("EXECUTE_TRADE | Position fetch failed — opposing guard will block")
                open_positions_list = None

            is_safe, reasons = self._safety.is_safe_to_trade(
                account_balance=float(balance),
                open_positions=len(open_positions_list),
                mt5_connected=bool(self._executor.is_connected()) if hasattr(self._executor, "is_connected") else True,
                has_high_impact_news=False,
                trade_direction=dir_s,
                open_positions_list=open_positions_list,
            )
            if not is_safe:
                self._log_tool("execute_trade", start, f"{dir_s} | REJECTED | safety: {'; '.join(reasons[:3])}")
                return {"success": False, "reason": "; ".join(reasons[:3])}

            # Risk sizing (max 2% enforced by config via caller; we use configured RISK_PER_TRADE)
            try:
                import config

                risk_pct = float(getattr(config, "RISK_PER_TRADE", 2.0))
            except Exception:
                risk_pct = 2.0

            pos = self._risk.calculate_position_size(
                account_balance=float(balance),
                risk_percent=risk_pct,
                stop_loss_pips=float(sl_pips),
            )

            # FLO-263: Cancel all pending orders before market execution (OCO safety)
            try:
                _pending = self._executor.get_pending_orders()
                if _pending:
                    _cancelled = self._executor.cancel_all_pending()
                    log.info(f"PENDING_ORDER | MARKET_OVERRIDE | execute_trade called → cancelled {_cancelled.get('cancelled', 0)} pending orders")
            except Exception:
                pass

            # Execute
            try:
                comment = f"Agent-{dir_s}"
                res = self._executor.execute_trade(
                    direction=dir_s,
                    lot_size=float(pos.lot_size),
                    stop_loss=float(sl_f),
                    take_profit=float(tp_f),
                    comment=comment,
                    confidence=None,
                    scenario="agent_tool",
                    risk_amount=float(pos.risk_amount),
                    risk_percent=float(risk_pct),
                )
            except Exception as e_exec:
                self._log_tool("execute_trade", start, f"{dir_s} | error={e_exec}")
                return {"success": False, "reason": "execution error"}

            if not getattr(res, "success", False):
                reason = getattr(res, "error_message", None) or "execution failed"
                self._log_tool("execute_trade", start, f"{dir_s} | success=false | {reason}")
                return {
                    "success": False,
                    "reason": str(reason),
                    "suggestion": "Price may have moved. Consider place_pending_order at your target level instead of retrying market order.",
                }

            fill_price = self._safe_float(getattr(res, "price", None))
            ticket = getattr(res, "ticket", None)

            # FLO-114: Guard against phantom trades — ticket must be a real positive int
            if not ticket or (isinstance(ticket, (int, float)) and int(ticket) <= 0):
                reason = getattr(res, "error_message", None) or "ticket_not_resolved"
                self._log_tool("execute_trade", start, f"{dir_s} | REJECTED | ticket={ticket} ({reason})")
                return {
                    "success": False,
                    "reason": str(reason),
                    "suggestion": "Execution failed. Consider place_pending_order at your target level instead of retrying.",
                }

            # FLO-63: Save trade conditions snapshot at open time
            if ticket is not None:
                try:
                    from trade_lessons import save_trade_conditions
                    indicators = self.get_indicators() if dp else {}
                    luna_ctx = {}
                    try:
                        from luna_analyst import load_luna_brief
                        lb = load_luna_brief()
                        if lb:
                            luna_ctx = {
                                "luna_environment": lb.get("environment"),
                                "luna_risk_level": lb.get("risk_level"),
                                "luna_bias": lb.get("directional_bias"),
                            }
                    except Exception:
                        pass

                    utc_hour = None
                    try:
                        utc_hour = datetime.utcnow().hour
                    except Exception:
                        pass

                    rex_agreed = None
                    try:
                        hist = getattr(self, "_rex_debate_history", [])
                        if hist:
                            rex_agreed = hist[-1].get("agree")
                    except Exception:
                        pass

                    conds = {
                        "rsi_h1": self._safe_float((indicators.get("rsi") or {}).get("value") if isinstance(indicators.get("rsi"), dict) else indicators.get("rsi")),
                        "macd_h1": self._safe_float((indicators.get("macd") or {}).get("value") if isinstance(indicators.get("macd"), dict) else indicators.get("macd")),
                        "adx_h1": self._safe_float((indicators.get("adx") or {}).get("value") if isinstance(indicators.get("adx"), dict) else indicators.get("adx")),
                        "atr_h1": self._safe_float((indicators.get("atr") or {}).get("value") if isinstance(indicators.get("atr"), dict) else indicators.get("atr")),
                        "ema50_distance_pct": None,
                        "volume_h1": self._safe_float(indicators.get("volume")),
                        "session": self._infer_session_from_utc_hour(utc_hour),
                        "utc_hour": utc_hour,
                        "confidence": self._safe_float(agent_confidence),
                        "rex_agreed": rex_agreed,
                    }
                    # EMA50 distance %
                    try:
                        ema50 = self._safe_float(indicators.get("ema50"))
                        if ema50 and fill_price:
                            conds["ema50_distance_pct"] = round(((fill_price - ema50) / ema50) * 100, 2)
                    except Exception:
                        pass
                    conds.update(luna_ctx)

                    # FLO-177: snapshot market regime at trade open
                    try:
                        _bs_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "bot_state.json")
                        if os.path.exists(_bs_path):
                            with open(_bs_path, "r", encoding="utf-8") as _bsf:
                                _bs = json.load(_bsf)
                            _mr = _bs.get("market_regime") or {}
                            if isinstance(_mr, dict) and _mr.get("regime"):
                                conds["regime"] = _mr["regime"]
                    except Exception:
                        pass

                    # FLO-137: snapshot active thesis at trade open
                    try:
                        _thesis_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "active_thesis.json")
                        if os.path.exists(_thesis_path):
                            with open(_thesis_path, "r", encoding="utf-8") as _tf:
                                _thesis = json.load(_tf)
                            conds["thesis_at_open"] = {
                                "direction_bias": _thesis.get("direction_bias"),
                                "key_levels": _thesis.get("key_levels"),
                                "conditions": _thesis.get("conditions"),
                                "decision": _thesis.get("decision"),
                                "confidence": _thesis.get("confidence"),
                            }
                    except Exception:
                        pass

                    # FLO-137: snapshot Rex debate reasoning at trade open
                    try:
                        hist = getattr(self, "_rex_debate_history", [])
                        if hist:
                            last_rex = hist[-1]
                            conds["rex_at_open"] = {
                                "agree": last_rex.get("agree"),
                                "reasoning": (last_rex.get("rex") or "")[:2000],
                            }
                    except Exception:
                        pass

                    save_trade_conditions(ticket, dir_s, conds)
                except Exception:
                    pass

            try:
                last_ts = getattr(self, "_rex_debate_last_ts", None)
                turns = int(getattr(self, "_rex_debate_turns", 0) or 0)
                if last_ts is not None and (time.time() - float(last_ts)) <= 300 and turns > 0:
                    log.info(f"DEBATE | complete | {turns} turns | outcome=EXECUTE")
            except Exception:
                pass

            self._log_tool(
                "execute_trade",
                start,
                f"{dir_s} @ {fill_price} | ticket={ticket} | success",
            )

            return {
                "success": True,
                "ticket": int(ticket) if ticket is not None else None,
                "fill_price": fill_price,
                "volume": float(pos.lot_size),
                "direction": dir_s,
                "sl": float(sl_f),
                "tp": float(tp_f),
                "warning": m5_warning,
            }
        except Exception as e:
            self._log_tool("execute_trade", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    def close_trade(self, ticket: int) -> Dict[str, Any]:
        start = time.time()
        try:
            try:
                t = int(ticket)
            except Exception:
                return {"success": False, "reason": "invalid ticket"}

            res = self._executor.close_position(t)
            if not getattr(res, "success", False):
                reason = getattr(res, "error_message", None) or "close failed"
                self._log_tool("close_trade", start, f"ticket={t} | success=false | {reason}")
                return {"success": False, "reason": str(reason)}

            close_price = self._safe_float(getattr(res, "price", None))
            self._log_tool("close_trade", start, f"ticket={t} | success")
            return {"success": True, "close_price": close_price, "profit": None}
        except Exception as e:
            self._log_tool("close_trade", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    # -----------------------------------------------------------------
    # FLO-141: adjust_trade guards
    # -----------------------------------------------------------------

    @staticmethod
    def _is_sl_widening(direction_type: int, old_sl: float, new_sl: float) -> bool:
        """Check if new SL widens risk (moves SL further from entry).
        direction_type: 0=BUY, 1=SELL (MT5 convention)."""
        if direction_type == 0:  # BUY — SL is below entry, widening = moving lower
            return new_sl < old_sl
        else:  # SELL — SL is above entry, widening = moving higher
            return new_sl > old_sl

    @staticmethod
    def _is_adjust_rate_limited(ticket: int, max_per_hour: int = 3) -> bool:
        """Check if ticket has exceeded max adjustments in the last rolling hour."""
        now = time.time()
        cutoff = now - 3600
        history = _adjust_rate_history.get(ticket, [])
        # Prune stale entries
        history = [ts for ts in history if ts > cutoff]
        _adjust_rate_history[ticket] = history
        return len(history) >= max_per_hour

    @staticmethod
    def _record_adjustment(ticket: int) -> None:
        """Record a successful adjustment timestamp."""
        _adjust_rate_history.setdefault(ticket, []).append(time.time())

    def adjust_trade(self, ticket: int, new_sl: float, new_tp: float) -> Dict[str, Any]:
        """Adjust SL/TP on an open position with SL-widening guard and rate limiting (FLO-141)."""
        start = time.time()
        try:
            try:
                t = int(ticket)
            except Exception:
                return {"success": False, "reason": "invalid ticket"}

            if t <= 0:
                self._log_tool("adjust_trade", start, f"ticket={t} | blocked | invalid_ticket")
                return {"success": False, "reason": "invalid ticket"}

            sl_f = self._safe_float(new_sl)
            tp_f = self._safe_float(new_tp)
            if sl_f is None and tp_f is None:
                return {"success": False, "reason": "invalid new sl/tp"}

            # FLO-200: adjust rate limit REMOVED — Floki has full autonomy
            # (was: 3/hour max, cost $22 on 2026-04-02 when blocked at 15:44)

            # --- Get current position (live MT5) for old values + direction ---
            old_sl = None
            old_tp = None
            direction_type = None  # 0=BUY, 1=SELL
            try:
                positions = self._executor.get_open_positions() or []
                for p in positions:
                    if getattr(p, "ticket", None) == t:
                        old_sl = self._safe_float(getattr(p, "sl", None))
                        old_tp = self._safe_float(getattr(p, "tp", None))
                        direction_type = getattr(p, "type", None)
                        break
            except Exception:
                pass

            # FLO-200: SL widening guard REMOVED — Floki has full autonomy
            # (was: blocked SL moves further from entry)

            # --- Execute modification ---
            res = self._executor.modify_position(t, new_sl=sl_f, new_tp=tp_f)
            if not getattr(res, "success", False):
                reason = getattr(res, "error_message", None) or "adjust failed"
                self._log_tool("adjust_trade", start, f"ticket={t} | success=false | {reason}")
                return {"success": False, "reason": str(reason)}

            # Record successful adjustment for rate limiting
            self._record_adjustment(t)

            # FLO-269: Record SL/TP adjustment for post-trade report
            try:
                from db_writer import record_trade_adjustment
                record_trade_adjustment(
                    ticket=t, old_sl=old_sl, new_sl=sl_f,
                    old_tp=old_tp, new_tp=tp_f, source="floki_adjust",
                )
            except Exception:
                pass

            _fmt = lambda v: f"{v:.2f}" if v is not None else "—"
            log.info(
                f"ADJUST_TRADE | SL: {_fmt(old_sl)}→{_fmt(sl_f)} | "
                f"TP: {_fmt(old_tp)}→{_fmt(tp_f)} | ticket={t}"
            )

            self._log_tool("adjust_trade", start, f"ticket={t} | success")
            return {
                "success": True,
                "ticket": t,
                "new_sl": sl_f,
                "new_tp": tp_f,
            }
        except Exception as e:
            self._log_tool("adjust_trade", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    # ---------------------------------------------------------------------
    # Session memory tools
    # ---------------------------------------------------------------------

    def read_session_memory(self) -> Dict[str, Any]:
        start = time.time()
        try:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            data_dir = os.path.join(base_dir, "data")
            mem_path = os.path.join(data_dir, "agent_session_memory.json")

            if not os.path.exists(mem_path):
                self._log_tool("read_session_memory", start, "empty")
                return {"empty": True}

            try:
                with open(mem_path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
            except Exception:
                self._log_tool("read_session_memory", start, "error=invalid_json")
                return {"empty": True}

            if not isinstance(payload, dict) or not payload:
                self._log_tool("read_session_memory", start, "empty")
                return {"empty": True}

            try:
                # FLO-309: session boundary uses UTC midnight via
                # trading_day_utc (was local midnight from datetime.now()).
                # For CEST users that shifts the rollover ~2h earlier.
                from tz_utils import trading_day_utc as _tday
                today = _tday()
                if str(payload.get("session_date") or "") != today:
                    payload["session_date"] = today
                    payload["notes"] = []
                    payload["last_updated"] = utc_iso()
            except Exception:
                pass

            self._log_tool("read_session_memory", start)
            return payload
        except Exception as e:
            self._log_tool("read_session_memory", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    def get_trade_patterns(self) -> Dict[str, Any]:
        """Return learned pattern memory + context + counter-examples.

        This reads the latest patterns JSON produced by the reflection engine.
        """
        start = time.time()
        try:
            try:
                from agent_reflection import read_patterns
            except Exception:
                self._log_tool("get_trade_patterns", start, "error=import_failed")
                return {"success": False, "reason": "patterns_unavailable"}

            payload = read_patterns()
            if not isinstance(payload, dict) or not payload:
                self._log_tool("get_trade_patterns", start, "error=invalid_payload")
                return {"success": False, "reason": "patterns_unavailable"}

            if payload.get("success") is False:
                self._log_tool("get_trade_patterns", start, f"reason={payload.get('reason')}")
                return payload

            patterns = payload.get("patterns") if isinstance(payload.get("patterns"), list) else []

            context = self._extract_context_for_patterns()
            counter_examples = self._query_similar_losing_trades(context, limit=2)

            out = dict(payload)
            out["context"] = context
            out["counter_examples"] = counter_examples

            self._log_tool(
                "get_trade_patterns",
                start,
                f"patterns={len(patterns)} counter_examples={len(counter_examples)}",
            )
            return out
        except Exception as e:
            self._log_tool("get_trade_patterns", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    def get_trade_lessons(self) -> Dict[str, Any]:
        """Return dynamic lessons from past trades (FLO-63)."""
        start = time.time()
        try:
            from trade_lessons import get_relevant_lessons
            lessons = get_relevant_lessons(min_occurrences=3, limit=10)
            self._log_tool("get_trade_lessons", start, f"lessons={len(lessons)}")
            return {
                "success": True,
                "lessons": lessons,
                "total": len(lessons),
            }
        except Exception as e:
            self._log_tool("get_trade_lessons", start, f"error={e}")
            return {"success": False, "reason": "lessons_unavailable"}

    # -----------------------------------------------------------------
    # FLO-137: Trade reflexion tools
    # -----------------------------------------------------------------

    def get_recent_reflexions(self, limit: int = 5) -> Dict[str, Any]:
        """Return the most recent post-trade reflexions (FLO-137)."""
        start = time.time()
        try:
            from db_writer import get_recent_reflexions as _get
            lim = min(max(int(limit or 5), 1), 20)
            rows = _get(lim)
            self._log_tool("get_recent_reflexions", start, f"count={len(rows)}")
            return {"success": True, "reflexions": rows, "count": len(rows)}
        except Exception as e:
            self._log_tool("get_recent_reflexions", start, f"error={e}")
            return {"success": False, "reason": "reflexions_unavailable"}

    def search_reflexions(self, keywords: str, limit: int = 5) -> Dict[str, Any]:
        """Search past trade reflexions by keywords (FLO-138)."""
        start = time.time()
        try:
            from db_writer import search_reflexions as _search
            kw = str(keywords or "").strip()
            if not kw:
                return {"success": False, "reason": "empty keywords"}
            lim = min(max(int(limit or 5), 1), 20)
            rows = _search(kw, lim)
            self._log_tool("search_reflexions", start, f"keywords={kw} | count={len(rows)}")
            return {"success": True, "results": rows, "count": len(rows)}
        except Exception as e:
            self._log_tool("search_reflexions", start, f"error={e}")
            return {"success": False, "reason": "search_unavailable"}

    def search_memory(self, query: str, limit: int = 3) -> Dict[str, Any]:
        """Semantic search across trade reflexions using embeddings (FLO-138 Phase 2)."""
        start = time.time()
        try:
            from trade_reflexion import search_memory as _semantic_search
            q = str(query or "").strip()
            if not q:
                return {"success": False, "reason": "empty query"}
            lim = min(max(int(limit or 3), 1), 10)
            results = _semantic_search(q, lim)
            if not results:
                # Fallback hint
                self._log_tool("search_memory", start, "chromadb_empty_or_unavailable")
                return {
                    "success": False,
                    "reason": "chromadb_unavailable",
                    "fallback": "use search_reflexions for keyword search",
                }
            self._log_tool("search_memory", start, f"query={q[:50]} | count={len(results)}")
            return {"success": True, "results": results, "count": len(results)}
        except Exception as e:
            self._log_tool("search_memory", start, f"error={e}")
            return {
                "success": False,
                "reason": "search_memory_error",
                "fallback": "use search_reflexions for keyword search",
            }

    # -----------------------------------------------------------------
    # FLO-269: Trade Journal — full trade history with MFE/MAE/adjustments
    # -----------------------------------------------------------------

    def get_trade_journal(
        self, limit: int = 20, session_filter: str = "", direction_filter: str = ""
    ) -> Dict[str, Any]:
        """Return detailed trade journal with MFE, capture rate, SL adjustments, and counterfactuals."""
        start = time.time()
        try:
            import json as _json
            import os as _os
            import sqlite3
            import config as _cfg
            from db_writer import _get_connection, get_trade_adjustments

            lim = min(max(int(limit or 20), 1), 30)

            conn = _get_connection()
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT ticket, direction, volume, open_price, close_price, sl, tp, "
                "profit, close_reason, open_time, close_time, mfe_points, mae_points, "
                "final_sl, breakeven_activated, decision_source, comment "  # FLO-301: comment for entry_type
                "FROM trades WHERE close_price IS NOT NULL AND profit IS NOT NULL "
                "ORDER BY close_time DESC LIMIT ?",
                (lim * 2,),  # fetch extra to allow filtering
            ).fetchall()

            # FLO-300: pull OPEN_* decisions once so we can attach opening
            # confidence to each trade below without N extra DB roundtrips.
            try:
                _open_decisions = conn.execute(
                    "SELECT timestamp, agent_decision, agent_confidence "
                    "FROM agent_proactive_analyses "
                    "WHERE agent_decision IN ('OPEN_BUY', 'OPEN_SELL') "
                    "ORDER BY timestamp ASC"
                ).fetchall()
            except Exception:
                _open_decisions = []
            conn.close()

            from datetime import datetime as _dt
            def _find_open_conf(direction, open_time_iso):
                if not direction or not open_time_iso:
                    return None
                want = "OPEN_BUY" if str(direction).upper() == "BUY" else "OPEN_SELL"
                try:
                    t_target = _dt.fromisoformat(str(open_time_iso).rstrip("Z").split(".")[0])
                except Exception:
                    return None
                best = None; best_delta = None
                for d in _open_decisions:
                    if d["agent_decision"] != want:
                        continue
                    try:
                        t_dec = _dt.fromisoformat(str(d["timestamp"]).rstrip("Z").split(".")[0])
                    except Exception:
                        continue
                    delta = abs((t_target - t_dec).total_seconds())
                    if delta > 600:
                        continue
                    if best_delta is None or delta < best_delta:
                        best_delta = delta; best = d["agent_confidence"]
                return best

            # Session helper (same as sage_auditor corrected logic)
            _offset = int(getattr(_cfg, "MT5_SERVER_UTC_OFFSET", 2) or 2)

            def _session(ts):
                try:
                    from datetime import datetime as _dt
                    d = _dt.fromisoformat((ts or "").split(".")[0])
                    h = (d.hour - _offset) % 24
                    if 0 <= h < 7:
                        return "Asian"
                    if 7 <= h < 13:
                        return "London"
                    if 13 <= h < 22:
                        return "NY"
                    return "OffHours"
                except Exception:
                    return "?"

            # Filter + build
            sess_f = (session_filter or "").strip().upper()
            dir_f = (direction_filter or "").strip().upper()
            reports_dir = _os.path.join(
                _os.path.dirname(_os.path.abspath(__file__)), "data", "post_trade_reports"
            )

            trades_xml = []
            total_capture = []
            adj_helped = 0
            adj_hurt = 0
            adj_neutral = 0
            count = 0
            _conf_outcomes = []   # FLO-300: (open_conf, pnl) pairs for band stats
            _et_outcomes = {"MARKET": [0, 0], "PENDING": [0, 0]}  # FLO-301: [trades, wins]

            for r in rows:
                if count >= lim:
                    break
                t = dict(r)
                ticket = t["ticket"]
                direction = t.get("direction", "?")
                sess_open = _session(t.get("open_time"))
                sess_close = _session(t.get("close_time"))

                if sess_f and sess_f not in (sess_open.upper(), sess_close.upper()):
                    continue
                if dir_f and direction.upper() != dir_f:
                    continue

                count += 1
                pnl = t.get("profit") or 0
                mfe = t.get("mfe_points")
                mae = t.get("mae_points")
                final_sl = t.get("final_sl")
                orig_sl = t.get("sl")

                # Capture rate — FLO-290: pips/pips (was dollars/pips bug).
                # FLO-300: display helper clamps extremes and shows "LOSS" for
                # noise-floor-small-MFE losses (previously rendered as "-6100%").
                from capture import compute_capture_pct, pnl_pips as _pnl_pips, format_capture_display
                capture = compute_capture_pct(
                    direction=t.get("direction"),
                    entry_price=t.get("open_price"),
                    close_price=t.get("close_price"),
                    mfe_points=mfe,
                )
                _pp = _pnl_pips(t.get("direction"), t.get("open_price"), t.get("close_price"))
                _capture_str = format_capture_display(capture, mfe, _pp)
                if capture is not None and mfe is not None and mfe > 0:
                    total_capture.append(capture)
                # FLO-301: detect PENDING vs MARKET from MT5 comment column.
                _cmt = (t.get("comment") or "")
                _entry_type = "PENDING" if ("pending" in _cmt.lower()) else "MARKET"
                _et_outcomes[_entry_type][0] += 1
                if (pnl or 0) > 0:
                    _et_outcomes[_entry_type][1] += 1

                # FLO-300/301: opening confidence ONLY for market orders. Pending
                # fills happen hours after the decision → attributing confidence
                # is misleading, so open_conf=None and XML renders "P.O."
                if _entry_type == "MARKET":
                    _open_conf = _find_open_conf(t.get("direction"), t.get("open_time"))
                    if _open_conf is not None:
                        _conf_outcomes.append((int(_open_conf), float(pnl or 0)))
                else:
                    _open_conf = None

                # Adjustments
                adjustments = get_trade_adjustments(int(ticket))
                if adjustments:
                    orig_sl = adjustments[0].get("old_sl") or orig_sl

                # Duration
                dur = ""
                try:
                    from datetime import datetime as _dt
                    od = _dt.fromisoformat((t.get("open_time") or "").split(".")[0])
                    cd = _dt.fromisoformat((t.get("close_time") or "").split(".")[0])
                    dur = f"{round((cd - od).total_seconds() / 60)}min"
                except Exception:
                    pass

                # Load counterfactual + MFE snapshot from report JSON
                cf = None
                mfe_snap = None
                report_path = _os.path.join(reports_dir, f"{ticket}.json")
                if _os.path.exists(report_path):
                    try:
                        with open(report_path, "r", encoding="utf-8") as f:
                            report_data = _json.load(f)
                        cf = report_data.get("counterfactual")
                        mfe_snap = report_data.get("mfe_snapshot")
                    except Exception:
                        pass

                # Verdict: compare actual outcome to counterfactual
                # Skip if no valid SL data (orig_sl=0 or None = reconciled trade with missing data)
                verdict = ""
                if cf and orig_sl is not None and float(orig_sl) > 0:
                    sl_survived = cf.get("original_sl_survived")
                    tp_hit = cf.get("tp_would_have_been_hit")
                    tp_pnl = cf.get("tp_hit_pnl")
                    entry_f = float(t.get("open_price") or 0)

                    if sl_survived is False and entry_f > 0:
                        # Original SL would have been hit — compute P&L if held to SL
                        orig_sl_f = float(orig_sl)
                        if direction.upper() == "BUY":
                            pnl_if_original = orig_sl_f - entry_f  # negative (loss)
                        else:
                            pnl_if_original = entry_f - orig_sl_f  # negative (loss)
                        diff = round(float(pnl) - pnl_if_original, 2)
                        if diff > 0:
                            verdict = f"SAVED ${diff:.2f}"
                            adj_helped += 1
                        elif diff < 0:
                            verdict = f"COST ${abs(diff):.2f}"
                            adj_hurt += 1
                        else:
                            verdict = "NEUTRAL"
                            adj_neutral += 1
                    elif tp_hit and tp_pnl is not None:
                        # TP would have been hit = actual close left money on table
                        cost = round(float(tp_pnl) - float(pnl), 2)
                        if cost > 0:
                            verdict = f"COST ${cost:.2f}"
                            adj_hurt += 1
                        else:
                            verdict = "NEUTRAL"
                            adj_neutral += 1
                    elif sl_survived is True and not tp_hit:
                        verdict = "NEUTRAL"
                        adj_neutral += 1

                # Format trade XML.
                # FLO-300: capture uses display helper (clamped / "LOSS").
                # FLO-301: entry_type + open_conf distinguishes pending orders
                # ("P.O.") from market orders ("52%"). Pending-order confidence
                # isn't comparable because the fill happens hours after decision.
                _f = lambda v, d=2: f"{float(v):.{d}f}" if v is not None else "?"
                if _entry_type == "PENDING":
                    _oc_str = "P.O."
                else:
                    _oc_str = f"{int(_open_conf)}%" if _open_conf is not None else "?"
                line = (
                    f'  <trade ticket="{ticket}" dir="{direction}" entry_type="{_entry_type}" '
                    f'session="{sess_open}->{sess_close}" '
                    f'pnl="${_f(pnl)}" mfe="{_f(mfe, 1)}pts" mae="{_f(mae, 1)}pts" '
                    f'capture="{_capture_str}" open_conf="{_oc_str}" '
                    f'entry="{_f(t.get("open_price"))}" orig_sl="{_f(orig_sl)}" '
                    f'final_sl="{_f(final_sl)}" tp="{_f(t.get("tp"))}" '
                    f'close="{_f(t.get("close_price"))}" type="{t.get("close_reason", "?")}" '
                    f'duration="{dur}"'
                )

                # Pre-check whether MFE snapshot will be rendered (same filter as below)
                _will_show_mfe = bool(
                    mfe_snap and (pnl < 0 or (capture is not None and capture < 50))
                )

                if not adjustments and not cf and not _will_show_mfe:
                    line += "/>"
                    trades_xml.append(line)
                    continue

                line += ">"
                trades_xml.append(line)

                # Adjustments sub-elements
                if adjustments:
                    trades_xml.append(f'    <adjustments count="{len(adjustments)}">')
                    for a in adjustments:
                        mins = ""
                        try:
                            from datetime import datetime as _dt
                            ot = _dt.fromisoformat((t.get("open_time") or "").split(".")[0])
                            at = _dt.fromisoformat((a.get("timestamp") or "").split(".")[0])
                            mins = f'{round((at - ot).total_seconds() / 60)}min'
                        except Exception:
                            pass
                        sl_part = f'sl="{_f(a.get("old_sl"))}->{_f(a.get("new_sl"))}"'
                        tp_part = ""
                        if a.get("new_tp") is not None and a.get("old_tp") != a.get("new_tp"):
                            tp_part = f' tp="{_f(a.get("old_tp"))}->{_f(a.get("new_tp"))}"'
                        trades_xml.append(
                            f'      <adj at="{mins}" {sl_part}{tp_part} source="{a.get("source", "?")}"/>'
                        )
                    trades_xml.append("    </adjustments>")

                # Counterfactual (rich detail)
                if cf:
                    cf_attrs = []
                    if cf.get("original_sl_survived") is True:
                        cf_attrs.append('orig_sl="survived"')
                    elif cf.get("original_sl_survived") is False:
                        hit_time = cf.get("original_sl_hit_time", "?")
                        # Extract just HH:MM from ISO timestamp
                        try:
                            hit_time = hit_time[11:16]
                        except Exception:
                            pass
                        cf_attrs.append(f'orig_sl="hit at {hit_time}"')
                    if cf.get("tp_would_have_been_hit"):
                        tp_time = cf.get("tp_hit_time", "?")
                        try:
                            tp_time = tp_time[11:16]
                        except Exception:
                            pass
                        cf_attrs.append(f'tp="hit at {tp_time} = +${cf.get("tp_hit_pnl")}"')
                    elif cf.get("tp_reached_after_sl"):
                        after_time = cf.get("tp_reached_after_sl_time", "?")
                        after_pnl = cf.get("tp_reached_after_sl_pnl")
                        try:
                            after_time = after_time[11:16]
                        except Exception:
                            pass
                        cf_attrs.append(f'tp="reached at {after_time} = +${after_pnl} BUT after SL hit"')
                    else:
                        cf_attrs.append('tp="never reached"')
                    hours = cf.get("hours_of_data", 0)
                    cf_attrs.append(f'window="{hours:.0f}h"')
                    cf_attrs.append(f'verdict="{verdict}"')
                    trades_xml.append(f'    <counterfactual {" ".join(cf_attrs)}/>')

                # FLO-273: MFE snapshot — show indicator state at peak profit
                # Only for losing trades or trades with low capture rate (where MFE matters)
                show_mfe = False
                if mfe_snap:
                    if pnl < 0:
                        show_mfe = True
                    elif capture is not None and capture < 50:
                        show_mfe = True

                if show_mfe and mfe_snap:
                    m_attrs = []
                    _mfe_pips = mfe_snap.get("profit_pips")
                    if _mfe_pips is not None:
                        m_attrs.append(f'at="+{_mfe_pips}pts"')
                    _mfe_time = mfe_snap.get("timestamp", "")
                    try:
                        _mfe_time_short = _mfe_time[11:16] if _mfe_time else ""
                        if _mfe_time_short:
                            m_attrs.append(f'time="{_mfe_time_short}"')
                    except Exception:
                        pass
                    if mfe_snap.get("rsi") is not None:
                        m_attrs.append(f'rsi="{mfe_snap["rsi"]}"')
                    _sk = mfe_snap.get("stochastic_k")
                    _sd = mfe_snap.get("stochastic_d")
                    if _sk is not None and _sd is not None:
                        m_attrs.append(f'stoch="{_sk}/{_sd}"')
                    elif _sk is not None:
                        m_attrs.append(f'stoch="{_sk}"')
                    if mfe_snap.get("adx") is not None:
                        m_attrs.append(f'adx="{mfe_snap["adx"]}"')
                    if mfe_snap.get("volume_ratio") is not None:
                        m_attrs.append(f'vol="{mfe_snap["volume_ratio"]}x"')
                    if mfe_snap.get("macd_histogram") is not None:
                        m_attrs.append(f'macd_h="{mfe_snap["macd_histogram"]}"')
                    if mfe_snap.get("bb_position"):
                        m_attrs.append(f'bb="{mfe_snap["bb_position"]}"')
                    if mfe_snap.get("nearest_sr"):
                        m_attrs.append(f'sr="{mfe_snap["nearest_sr"]}"')
                    if mfe_snap.get("regime"):
                        m_attrs.append(f'regime="{mfe_snap["regime"]}"')
                    _fd = mfe_snap.get("floki_decision_at_mfe")
                    _fc = mfe_snap.get("floki_confidence_at_mfe")
                    if _fd:
                        _fc_str = f" ({_fc}%)" if _fc is not None else ""
                        m_attrs.append(f'floki_said="{_fd}{_fc_str}"')
                    trades_xml.append(f'    <mfe_snapshot {" ".join(m_attrs)}/>')

                trades_xml.append("  </trade>")

            # Header stats
            avg_cap = round(sum(total_capture) / len(total_capture), 1) if total_capture else None
            total_adj_trades = adj_helped + adj_hurt + adj_neutral
            helped_pct = round(adj_helped / total_adj_trades * 100) if total_adj_trades > 0 else None
            hurt_pct = round(adj_hurt / total_adj_trades * 100) if total_adj_trades > 0 else None

            # FLO-300: win-rate-by-confidence-band summary for Floki to learn from.
            # Bands: <50, 50-65, 65+. "Win" = pnl > 0.
            _bands = {"lt50": [0,50,0,0], "mid": [50,65,0,0], "ge65": [65,101,0,0]}
            for _c, _p in _conf_outcomes:
                for _b in _bands.values():
                    if _b[0] <= _c < _b[1]:
                        _b[2] += 1           # trades
                        if _p > 0: _b[3] += 1  # wins
                        break
            def _band_attr(b):
                if not b[2]: return None
                return f"{b[3]}/{b[2]} ({round(b[3]/b[2]*100)}%)"
            _lt50 = _band_attr(_bands["lt50"])
            _mid  = _band_attr(_bands["mid"])
            _ge65 = _band_attr(_bands["ge65"])

            header = f'<trade_journal count="{count}"'
            if avg_cap is not None:
                header += f' avg_capture="{avg_cap}%"'
            if helped_pct is not None:
                header += f' adj_helped="{helped_pct}%" adj_hurt="{hurt_pct}%"'
            # FLO-301: market vs pending win-rate split, so Floki can compare
            # whether his pending orders perform better or worse than market
            # orders. Bands below are market-only (pending have no comparable
            # confidence value — see FLO-301 rationale).
            def _wr_attr(tw_pair):
                tr, wn = tw_pair
                return f"{wn}/{tr} ({round(wn/tr*100)}%)" if tr else None
            _mkt_wr = _wr_attr(_et_outcomes["MARKET"])
            _pnd_wr = _wr_attr(_et_outcomes["PENDING"])
            if _mkt_wr is not None: header += f' market_wr="{_mkt_wr}"'
            if _pnd_wr is not None: header += f' pending_wr="{_pnd_wr}"'
            # FLO-300: band breakdown (market orders only) — only emit bands that
            # have data, so Floki isn't misled by "0/0" slots.
            if _lt50 is not None: header += f' wr_lt50="{_lt50}"'
            if _mid  is not None: header += f' wr_50_65="{_mid}"'
            if _ge65 is not None: header += f' wr_65_plus="{_ge65}"'
            header += ">"

            xml = header + "\n" + "\n".join(trades_xml) + "\n</trade_journal>"

            self._log_tool("get_trade_journal", start, f"count={count} avg_cap={avg_cap}")
            return {"success": True, "journal": xml, "count": count}

        except Exception as e:
            self._log_tool("get_trade_journal", start, f"error={e}")
            return {"success": False, "reason": f"journal_error: {e}"}

    # -----------------------------------------------------------------
    # FLO-281: Position history — indicator trajectory for an open trade
    # -----------------------------------------------------------------

    def get_position_history(self, ticket: int) -> Dict[str, Any]:
        """Return compact XML summary of how an open position has performed.

        Queries trade_snapshots for profit range, duration, trend direction,
        current indicators, and indicators at MFE peak. Floki calls this
        when he wants to review how his trade has been trending.
        """
        start = time.time()
        try:
            try:
                t = int(ticket)
            except Exception:
                self._log_tool("get_position_history", start, f"invalid ticket={ticket}")
                return {"success": False, "reason": "invalid ticket"}
            if t <= 0:
                self._log_tool("get_position_history", start, f"ticket={t} invalid")
                return {"success": False, "reason": "invalid ticket"}

            import sqlite3 as _sql
            import config as _cfg
            from datetime import datetime as _dt

            db_path = os.path.abspath(getattr(_cfg, "HISTORY_DB_PATH", "data/history.db"))
            conn = _sql.connect(db_path, timeout=5)
            conn.row_factory = _sql.Row

            # Aggregate stats
            agg = conn.execute(
                "SELECT COUNT(*) as n, MIN(profit_pips) as min_p, MAX(profit_pips) as max_p, "
                "MIN(timestamp) as first_ts, MAX(timestamp) as last_ts "
                "FROM trade_snapshots WHERE ticket = ? AND profit_pips IS NOT NULL",
                (t,),
            ).fetchone()
            n = agg["n"] if agg else 0
            if not n:
                conn.close()
                self._log_tool("get_position_history", start, f"ticket={t} no snapshots")
                return {"success": False, "reason": f"No snapshot history for ticket {t} — position may have just opened"}

            min_p = float(agg["min_p"]) if agg["min_p"] is not None else 0.0
            max_p = float(agg["max_p"]) if agg["max_p"] is not None else 0.0
            first_ts = agg["first_ts"]
            last_ts = agg["last_ts"]

            # MFE snapshot row (for indicators at peak)
            mfe_row = conn.execute(
                "SELECT * FROM trade_snapshots WHERE ticket = ? AND profit_pips IS NOT NULL "
                "ORDER BY profit_pips DESC LIMIT 1",
                (t,),
            ).fetchone()
            mfe_snap = dict(mfe_row) if mfe_row else {}

            # Most recent snapshot (for indicators now)
            now_row = conn.execute(
                "SELECT * FROM trade_snapshots WHERE ticket = ? "
                "ORDER BY timestamp DESC LIMIT 1",
                (t,),
            ).fetchone()
            now_snap = dict(now_row) if now_row else {}
            conn.close()

            current_p = float(now_snap.get("profit_pips") or 0.0)

            # Duration from first to last snapshot
            duration_str = "?"
            try:
                fd = _dt.fromisoformat(str(first_ts).replace("Z", "+00:00")) if first_ts else None
                ld = _dt.fromisoformat(str(last_ts).replace("Z", "+00:00")) if last_ts else None
                if fd and ld:
                    mins = int((ld - fd).total_seconds() / 60)
                    duration_str = f"{mins}m" if mins < 60 else f"{mins // 60}h{mins % 60}m"
            except Exception:
                pass

            # Simple trend classification — FLO-289: all numbers explicitly pips.
            range_p = max_p - min_p
            if range_p < 10:
                trend_dir = "flat"
                trend_desc = f"Oscillating between {min_p:+.1f} and {max_p:+.1f} pips for {duration_str}. No directional progress."
            elif current_p >= max_p * 0.8 and max_p > 0:
                trend_dir = "climbing"
                trend_desc = f"Near peak profit ({max_p:+.1f} pips). Currently {current_p:+.1f} pips."
            elif current_p <= min_p * 0.8 and min_p < 0:
                trend_dir = "losing_ground"
                trend_desc = f"Near worst drawdown ({min_p:+.1f} pips). Currently {current_p:+.1f} pips."
            elif max_p > 0 and current_p < max_p * 0.3:
                trend_dir = "gave_back_gains"
                trend_desc = f"Peaked at {max_p:+.1f} pips but fell back to {current_p:+.1f} pips. Gave back {max_p - current_p:.1f} pips."
            else:
                trend_dir = "mixed"
                trend_desc = f"Range {min_p:+.1f} to {max_p:+.1f} pips, currently {current_p:+.1f} pips."

            # Build XML
            def _attr(v):
                return "?" if v is None else str(v)

            def _fmt_num(v, d=1):
                try:
                    return f"{float(v):.{d}f}" if v is not None else "?"
                except Exception:
                    return "?"

            mfe_time_short = "?"
            try:
                _mt = str(mfe_snap.get("timestamp") or "")
                mfe_time_short = _mt[11:16] if _mt else "?"
            except Exception:
                pass

            lines = [
                f'<position_history ticket="{t}" duration="{duration_str}" snapshots="{n}">',
                # FLO-289: unit="pips" explicit — prevents Floki from reading
                # these as dollars (the unlabeled 63.8 was misread as "$63 peak").
                f'  <profit_range unit="pips" min="{_fmt_num(min_p)}" max="{_fmt_num(max_p)}" current="{_fmt_num(current_p)}"/>',
                f'  <trend direction="{trend_dir}" description="{trend_desc}"/>',
                f'  <indicators_now rsi="{_attr(now_snap.get("rsi"))}" '
                f'stoch_k="{_attr(now_snap.get("stochastic_k"))}" '
                f'adx="{_attr(now_snap.get("adx"))}" '
                f'regime="{_attr(now_snap.get("regime"))}" '
                f'nearest_sr="{_attr(now_snap.get("nearest_sr"))}"/>',
                f'  <indicators_at_mfe rsi="{_attr(mfe_snap.get("rsi"))}" '
                f'stoch_k="{_attr(mfe_snap.get("stochastic_k"))}" '
                f'adx="{_attr(mfe_snap.get("adx"))}" '
                f'regime="{_attr(mfe_snap.get("regime"))}" '
                f'at="{_fmt_num(mfe_snap.get("profit_pips"))}pts" '
                f'time="{mfe_time_short}"/>',
                "</position_history>",
            ]
            xml = "\n".join(lines)

            self._log_tool("get_position_history", start,
                           f"ticket={t} snapshots={n} range={min_p:+.1f}..{max_p:+.1f} trend={trend_dir}")
            return {"success": True, "history": xml, "snapshots": n}

        except Exception as e:
            self._log_tool("get_position_history", start, f"error={e}")
            return {"success": False, "reason": f"history_error: {e}"}

    # -----------------------------------------------------------------
    # FLO-158: Rex-unique tools (not available to Floki)
    # -----------------------------------------------------------------

    def rex_session_performance(self) -> Dict[str, Any]:
        """WR and PF by session + direction for recent agent trades."""
        start = time.time()
        try:
            from db_writer import _get_connection
            conn = _get_connection()
            try:
                rows = conn.execute("""
                    SELECT direction, close_reason, profit, open_time
                    FROM trades
                    WHERE close_time IS NOT NULL AND profit IS NOT NULL
                      AND decision_source IN ('floki_agent', 'agent_floki')
                      AND open_time >= datetime('now', '-30 days')
                """).fetchall()
            finally:
                conn.close()

            sessions = {"asian": {}, "london": {}, "ny": {}}
            for direction, _, profit, open_time in rows:
                try:
                    hour = int(open_time[11:13]) if open_time and len(open_time) > 13 else 12
                except Exception:
                    hour = 12
                if 22 <= hour or hour < 7:
                    sess = "asian"
                elif 7 <= hour < 13:
                    sess = "london"
                else:
                    sess = "ny"
                d = str(direction or "BUY").upper()
                key = d
                if key not in sessions[sess]:
                    sessions[sess][key] = {"wins": 0, "losses": 0, "total_pnl": 0.0}
                pnl = float(profit or 0)
                if pnl > 0:
                    sessions[sess][key]["wins"] += 1
                else:
                    sessions[sess][key]["losses"] += 1
                sessions[sess][key]["total_pnl"] += pnl

            result = {}
            for sess, directions in sessions.items():
                result[sess] = {}
                for d, stats in directions.items():
                    n = stats["wins"] + stats["losses"]
                    wr = round(stats["wins"] / n * 100, 1) if n > 0 else 0
                    result[sess][d] = {"wr": wr, "n": n, "pnl": round(stats["total_pnl"], 2)}
            self._log_tool("rex_session_performance", start, f"sessions={len(result)}")
            return {"success": True, "performance": result}
        except Exception as e:
            self._log_tool("rex_session_performance", start, f"error={e}")
            return {"success": False, "reason": str(e)}

    def rex_divergence_scan(self) -> Dict[str, Any]:
        """Scan for RSI/MACD divergences on H4 and D1."""
        start = time.time()
        try:
            import MetaTrader5 as mt5
            import numpy as np
            if not mt5.initialize():
                return {"success": False, "reason": "MT5 unavailable"}

            result = {}
            for tf_name, tf in [("H4", mt5.TIMEFRAME_H4), ("D1", mt5.TIMEFRAME_D1)]:
                bars = mt5.copy_rates_from_pos("XAUUSD", tf, 0, 20)
                if bars is None or len(bars) < 10:
                    result[tf_name] = {"rsi": "insufficient_data", "macd": "insufficient_data"}
                    continue

                closes = [float(b[4]) for b in bars]
                highs = [float(b[2]) for b in bars]
                lows = [float(b[3]) for b in bars]

                # RSI calculation (14-period)
                deltas = np.diff(closes)
                gains = np.where(deltas > 0, deltas, 0)
                losses = np.where(deltas < 0, -deltas, 0)
                avg_gain = np.mean(gains[-14:])
                avg_loss = np.mean(losses[-14:])
                rs = avg_gain / avg_loss if avg_loss > 0 else 100
                rsi_now = 100 - (100 / (1 + rs))

                # Check last 2 swing highs for bearish divergence
                rsi_div = "none"
                if len(closes) >= 10:
                    # Simple: compare price high vs RSI at recent peaks
                    ph1_idx = np.argmax(highs[-10:-5])
                    ph2_idx = np.argmax(highs[-5:]) + 5
                    if highs[ph2_idx + len(highs) - 10] > highs[ph1_idx + len(highs) - 10]:
                        # Price higher high — check if RSI lower
                        # Approximate RSI at each peak (simplified)
                        if rsi_now < 60 and closes[-1] > closes[-6]:
                            rsi_div = "bearish"
                    elif highs[ph2_idx + len(highs) - 10] < highs[ph1_idx + len(highs) - 10]:
                        if rsi_now > 40 and closes[-1] < closes[-6]:
                            rsi_div = "bullish"

                # MACD histogram divergence
                macd_div = "none"
                if len(closes) >= 26:
                    ema12_arr = [closes[0]]
                    ema26_arr = [closes[0]]
                    m12 = 2.0 / 13.0
                    m26 = 2.0 / 27.0
                    for c in closes[1:]:
                        ema12_arr.append(c * m12 + ema12_arr[-1] * (1 - m12))
                        ema26_arr.append(c * m26 + ema26_arr[-1] * (1 - m26))
                    macd_line = [e12 - e26 for e12, e26 in zip(ema12_arr, ema26_arr)]
                    signal = [macd_line[0]]
                    m9 = 2.0 / 10.0
                    for v in macd_line[1:]:
                        signal.append(v * m9 + signal[-1] * (1 - m9))
                    hist = [m - s for m, s in zip(macd_line, signal)]
                    # Compare histogram at recent swing highs/lows (last 10 bars split into 2 halves)
                    if len(hist) >= 10:
                        h1_peak = max(hist[-10:-5])
                        h2_peak = max(hist[-5:])
                        h1_trough = min(hist[-10:-5])
                        h2_trough = min(hist[-5:])
                        price_hh = max(highs[-5:]) > max(highs[-10:-5])
                        price_ll = min(lows[-5:]) < min(lows[-10:-5])
                        if price_hh and h2_peak < h1_peak and h1_peak > 0:
                            macd_div = "bearish"
                        elif price_ll and h2_trough > h1_trough and h1_trough < 0:
                            macd_div = "bullish"

                result[tf_name] = {
                    "rsi": rsi_div,
                    "rsi_value": round(rsi_now, 1),
                    "macd_divergence": macd_div,
                    "bars_analyzed": len(bars),
                }
            self._log_tool("rex_divergence_scan", start, f"H4={result.get('H4',{}).get('rsi')} D1={result.get('D1',{}).get('rsi')}")
            return {"success": True, "divergences": result}
        except Exception as e:
            self._log_tool("rex_divergence_scan", start, f"error={e}")
            return {"success": False, "reason": str(e)}

    def rex_regime_history(self) -> Dict[str, Any]:
        """Read regime state history — past transitions and durations."""
        start = time.time()
        try:
            import json as _json
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "regime_state.json")
            if not os.path.exists(path):
                return {"success": True, "current": None, "transitions": []}
            with open(path, "r", encoding="utf-8") as f:
                data = _json.load(f)
            regime = data.get("regime")
            change_ts = data.get("change_ts")
            history = data.get("history", [])[-10:]
            duration_min = None
            if change_ts:
                duration_min = int((time.time() - float(change_ts)) / 60)
            self._log_tool("rex_regime_history", start, f"regime={regime} transitions={len(history)}")
            return {
                "success": True,
                "current_regime": regime,
                "duration_minutes": duration_min,
                "recent_transitions": history,
            }
        except Exception as e:
            self._log_tool("rex_regime_history", start, f"error={e}")
            return {"success": False, "reason": str(e)}

    def rex_reflexion_search(self, query: str, limit: int = 3) -> Dict[str, Any]:
        """Semantic search past trade reflexions (ChromaDB)."""
        start = time.time()
        try:
            from trade_reflexion import search_memory as _search
            q = str(query or "").strip()
            if not q:
                return {"success": False, "reason": "empty query"}
            results = _search(q, min(max(int(limit or 3), 1), 10))
            self._log_tool("rex_reflexion_search", start, f"query={q[:30]} results={len(results)}")
            return {"success": True, "results": results, "count": len(results)}
        except Exception as e:
            self._log_tool("rex_reflexion_search", start, f"error={e}")
            return {"success": False, "reason": str(e)}

    def rex_correlation_check(self) -> Dict[str, Any]:
        """Real-time correlation check: gold vs DXY, yields, silver (last 24h H1)."""
        start = time.time()
        try:
            import MetaTrader5 as mt5
            import numpy as np
            if not mt5.initialize():
                return {"success": False, "reason": "MT5 unavailable"}

            pairs = {
                "gold_dxy": ("XAUUSD", "DXY_M6"),
                "gold_silver": ("XAUUSD", "XAGUSD"),
                "gold_10y": ("XAUUSD", "UST10Y_M6"),
            }
            normal_corr = {"gold_dxy": -0.60, "gold_silver": 0.85, "gold_10y": -0.50}
            result = {}
            gold_bars = mt5.copy_rates_from_pos("XAUUSD", mt5.TIMEFRAME_H1, 0, 24)
            if gold_bars is None or len(gold_bars) < 12:
                return {"success": False, "reason": "insufficient gold data"}
            gold_closes = np.array([float(b[4]) for b in gold_bars])

            for key, (_, other_sym) in pairs.items():
                other_bars = mt5.copy_rates_from_pos(other_sym, mt5.TIMEFRAME_H1, 0, 24)
                if other_bars is None or len(other_bars) < 12:
                    result[key] = {"status": "no_data"}
                    continue
                other_closes = np.array([float(b[4]) for b in other_bars])
                min_len = min(len(gold_closes), len(other_closes))
                if min_len < 12:
                    result[key] = {"status": "insufficient_overlap"}
                    continue
                corr = float(np.corrcoef(gold_closes[-min_len:], other_closes[-min_len:])[0, 1])
                norm = normal_corr.get(key, 0)
                broken = abs(corr - norm) > 0.4
                result[key] = {
                    "correlation": round(corr, 3),
                    "normal": norm,
                    "status": "BROKEN" if broken else "NORMAL",
                }
            self._log_tool("rex_correlation_check", start, f"pairs={len(result)}")
            return {"success": True, "correlations": result}
        except Exception as e:
            self._log_tool("rex_correlation_check", start, f"error={e}")
            return {"success": False, "reason": str(e)}

    def write_session_memory(self, thesis: str, note: str) -> Dict[str, Any]:
        start = time.time()
        try:
            thesis_s = str(thesis or "").strip()
            note_s = str(note or "").strip()
            if not thesis_s and not note_s:
                return {"success": False, "reason": "empty thesis/note"}

            base_dir = os.path.dirname(os.path.abspath(__file__))
            data_dir = os.path.join(base_dir, "data")
            mem_path = os.path.join(data_dir, "agent_session_memory.json")
            os.makedirs(data_dir, exist_ok=True)

            # FLO-309: session boundary → trading_day_utc (UTC midnight).
            from tz_utils import trading_day_utc as _tday
            today = _tday()
            payload: Dict[str, Any] = {
                "session_date": today,
                "thesis": thesis_s,
                "trades_today": 0,
                "wins_today": 0,
                "losses_today": 0,
                "notes": [],
                "last_updated": utc_iso(),
            }

            if os.path.exists(mem_path):
                try:
                    with open(mem_path, "r", encoding="utf-8") as f:
                        existing = json.load(f)
                    if isinstance(existing, dict):
                        payload.update(existing)
                except Exception:
                    pass

            # Daily rollover
            if str(payload.get("session_date") or "") != today:
                preserved_sage_notes = []
                try:
                    for n in payload.get("notes") or []:
                        if isinstance(n, dict) and str(n.get("source") or "").strip().lower() == "sage":
                            preserved_sage_notes.append(n)
                except Exception:
                    preserved_sage_notes = []
                payload = {
                    "session_date": today,
                    "thesis": thesis_s,
                    "trades_today": 0,
                    "wins_today": 0,
                    "losses_today": 0,
                    "notes": preserved_sage_notes,
                    "last_updated": utc_iso(),  # FLO-309 regression fix
                }

            if thesis_s:
                payload["thesis"] = thesis_s

            if not isinstance(payload.get("notes"), list):
                payload["notes"] = []

            if note_s:
                # FLO-241: Dedup — ALWAYS check, no exceptions. Reflection forces Floki to think.
                try:
                    import re as _re_sm
                    _SYN = {"middle": "center", "box": "range", "reclaim": "push",
                            "under": "below", "wake": "reassess", "business": "trade",
                            "unchanged": "same", "framework": "thesis", "lean": "consider",
                            "actionable": "tradeable", "acceptance": "confirmation",
                            "continuation": "extension", "opens": "targets",
                            "stay": "remain", "flat": "idle", "decisive": "clear",
                            "engage": "enter", "respect": "watch", "especially": "particularly"}
                    _STOP = {"a", "an", "the", "is", "in", "on", "of", "to", "for",
                             "and", "or", "but", "not", "this", "that", "with", "from",
                             "at", "by", "do", "if", "it", "my", "no", "so", "be", "i"}
                    def _sm_norm(s):
                        s = s.lower().strip()
                        s = _re_sm.sub(r'\d{4,}\.?\d*', 'PRICE', s)
                        s = _re_sm.sub(r'[.,;:!?()"\'\-/]', ' ', s)
                        s = _re_sm.sub(r'\s+', ' ', s)
                        words = [_SYN.get(w, w) for w in s.split() if w not in _STOP and len(w) > 1]
                        return ' '.join(words)
                    _new_norm = _sm_norm(note_s)[:120]
                    _new_words = set(_new_norm.split())
                    for _existing_n in (payload.get("notes") or []):
                        _ex_text = _existing_n.get("note", _existing_n.get("text", "")) if isinstance(_existing_n, dict) else str(_existing_n)
                        if isinstance(_existing_n, dict) and str(_existing_n.get("source") or "").lower() == "sage":
                            continue
                        _ex_norm = _sm_norm(_ex_text)[:120]
                        _ex_words = set(_ex_norm.split())
                        if _new_words and _ex_words:
                            _overlap = len(_new_words & _ex_words) / max(len(_new_words), len(_ex_words))
                            if _overlap >= 0.55:
                                self._log_tool("write_session_memory", start, "REJECTED (similar note exists)")
                                _rej_preview = []
                                try:
                                    for _rn in (payload.get("notes") or [])[-5:]:
                                        _rnt = _rn.get("note", _rn.get("text", "")) if isinstance(_rn, dict) else str(_rn)
                                        _rej_preview.append(_rnt[:80])
                                except Exception:
                                    pass
                                return {
                                    "saved": False,
                                    "reason": "You already have a similar note in your memory. "
                                              "Are you seeing the market the same way, or are you missing something new? "
                                              "Look again at what price is actually doing right now.",
                                    "your_recent_notes": _rej_preview,
                                }
                except Exception:
                    pass

                payload["notes"].append({"time": utc_now().strftime("%H:%M"), "note": note_s})  # FLO-309 regression fix

                # FLO-241: Cap at 8 notes (was 20). Protect Sage notes.
                try:
                    all_notes = payload.get("notes") or []
                    sage_notes = [n for n in all_notes if isinstance(n, dict) and str(n.get("source") or "").lower() == "sage"]
                    normal_notes = [n for n in all_notes if not (isinstance(n, dict) and str(n.get("source") or "").lower() == "sage")]
                    normal_notes = normal_notes[-7:]  # 7 normal + sage notes = ~8 total
                    payload["notes"] = normal_notes + sage_notes
                    payload["notes"] = payload["notes"][-8:]
                except Exception:
                    payload["notes"] = payload["notes"][-8:]

            payload["last_updated"] = utc_iso()  # FLO-309 regression fix

            try:
                with open(mem_path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
            except Exception:
                self._log_tool("write_session_memory", start, "error=write_failed")
                return {"success": False, "reason": "write failed"}

            # Return existing notes so Floki sees what he already wrote
            _existing_preview = []
            try:
                for _n in (payload.get("notes") or [])[-5:]:
                    _nt = _n.get("note", _n.get("text", "")) if isinstance(_n, dict) else str(_n)
                    _existing_preview.append(_nt[:80])
            except Exception:
                pass

            self._log_tool("write_session_memory", start, f"notes_count={len(payload.get('notes') or [])}")
            return {
                "saved": True,
                "notes_count": len(payload.get("notes") or []),
                "your_recent_notes": _existing_preview,
                "reminder": "Review your notes above. Next time, only write what is genuinely NEW.",
            }
        except Exception as e:
            self._log_tool("write_session_memory", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    def _write_json_atomic(self, path: str, payload: Any) -> bool:
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            if os.path.exists(path):
                os.remove(path)
            os.rename(tmp, path)
            return True
        except Exception:
            return False

    def _watch_conditions_path(self) -> str:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        data_dir = os.path.join(base_dir, "data")
        os.makedirs(data_dir, exist_ok=True)
        return os.path.join(data_dir, "agent_watch_conditions.json")

    def _next_check_path(self) -> str:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        data_dir = os.path.join(base_dir, "data")
        os.makedirs(data_dir, exist_ok=True)
        return os.path.join(data_dir, "agent_next_check.json")

    def _wake_conditions_path(self) -> str:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        data_dir = os.path.join(base_dir, "data")
        os.makedirs(data_dir, exist_ok=True)
        return os.path.join(data_dir, "agent_wake_conditions.json")

    # ---------------------------------------------------------------------
    # Echo News Sentinel alerts
    # ---------------------------------------------------------------------

    def get_echo_alerts(self) -> Dict[str, Any]:
        """Read unread Echo alerts (IMPORTANT/CRITICAL). Marks as read."""
        start = time.time()
        try:
            from echo_sentinel import get_unread_alerts
            alerts = get_unread_alerts()
            elapsed = round((time.time() - start) * 1000, 1)
            if not alerts:
                return {"success": True, "alerts": [], "count": 0, "latency_ms": elapsed}
            return {
                "success": True,
                "alerts": alerts,
                "count": len(alerts),
                "latency_ms": elapsed,
            }
        except Exception as e:
            elapsed = round((time.time() - start) * 1000, 1)
            return {"success": False, "reason": f"echo_alerts_error: {e}", "latency_ms": elapsed}

    # ---------------------------------------------------------------------
    # Luna Macro Analyst brief
    # ---------------------------------------------------------------------

    def get_luna_brief(self) -> Dict[str, Any]:
        """Read the latest Luna macro analysis brief."""
        start = time.time()
        try:
            from luna_analyst import load_luna_brief
            brief = load_luna_brief()
            elapsed = round((time.time() - start) * 1000, 1)

            if brief is None:
                return {"success": True, "brief": None, "stale": True, "latency_ms": elapsed}

            # Check freshness — flag if older than 30 min
            stale = False
            ts = brief.get("timestamp")
            if ts:
                try:
                    from datetime import datetime, timezone
                    brief_time = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    age_min = (datetime.now(timezone.utc) - brief_time).total_seconds() / 60
                    stale = age_min > 30
                    brief["age_minutes"] = round(age_min, 1)
                except Exception:
                    pass

            return {
                "success": True,
                "brief": brief,
                "stale": stale,
                "latency_ms": elapsed,
            }
        except Exception as e:
            elapsed = round((time.time() - start) * 1000, 1)
            return {"success": False, "reason": f"luna_brief_error: {e}", "latency_ms": elapsed}

    def get_rex_monitor(self) -> Dict[str, Any]:
        """Read latest Rex proactive monitoring scan (FLO-211)."""
        start = time.time()
        try:
            from rex_monitor import load_rex_monitor
            monitor = load_rex_monitor()
            elapsed = round((time.time() - start) * 1000, 1)

            if monitor is None:
                self._log_tool("get_rex_monitor", start, "empty/stale")
                return {"success": True, "monitor": None, "stale": True, "latency_ms": elapsed}

            stale = False
            age_minutes = None
            ts = monitor.get("timestamp")
            if ts:
                try:
                    from datetime import datetime, timezone
                    scan_time = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    age_minutes = round((datetime.now(timezone.utc) - scan_time).total_seconds() / 60, 1)
                    stale = age_minutes > 30
                except Exception:
                    pass

            # Return summary (not full raw_data — Floki doesn't need it)
            summary = {
                "alert_level": monitor.get("alert_level", "QUIET"),
                "finding_count": monitor.get("finding_count", 0),
                "findings": monitor.get("findings", []),
                "timestamp": ts,
            }

            # Refresh regime duration in findings (frozen at scan time)
            try:
                import re as _re
                regime_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "regime_state.json")
                if os.path.exists(regime_path):
                    import json as _json2
                    with open(regime_path, "r", encoding="utf-8") as _rf:
                        _rdata = _json2.load(_rf)
                    _cts = _rdata.get("change_ts")
                    if _cts:
                        _fresh_dur = int((time.time() - float(_cts)) / 60)
                        for _f in summary.get("findings", []):
                            if _f.get("type") == "REGIME_CHANGE":
                                _f["detail"] = _re.sub(r'\d+m ago$', f'{_fresh_dur}m ago', _f["detail"])
            except Exception:
                pass

            self._log_tool("get_rex_monitor", start, f"alert={summary['alert_level']} findings={summary['finding_count']}")
            return {
                "success": True,
                "monitor": summary,
                "stale": stale,
                "age_minutes": age_minutes,
                "latency_ms": elapsed,
            }
        except Exception as e:
            elapsed = round((time.time() - start) * 1000, 1)
            self._log_tool("get_rex_monitor", start, f"error={e}")
            return {"success": False, "reason": f"rex_monitor_error: {e}", "latency_ms": elapsed}

    # FLO-262: Available timeframes for chart screenshots
    _CHART_TFS = ["D1", "H4", "H1", "M15", "M5", "M1"]  # FLO-304: added M1

    def get_chart_screenshots(self, timeframes: list = None) -> Dict[str, Any]:
        """Return chart screenshots for requested timeframes. Images injected by caller.

        FLO-262: Accepts optional timeframes list (e.g. ['M5'], ['H4','D1']).
        If omitted, returns all available timeframes.
        """
        start = time.time()
        ci = getattr(self, '_chart_images', {}) or {}

        # Determine which TFs to return
        if timeframes and isinstance(timeframes, list):
            requested = [tf.upper().strip() for tf in timeframes if isinstance(tf, str)]
            requested = [tf for tf in requested if tf in self._CHART_TFS]
        else:
            requested = list(self._CHART_TFS)  # all available

        available = {}
        for tf in requested:
            key = f"{tf.lower()}_b64"
            if ci.get(key):
                available[tf] = len(ci[key]) // 1024  # KB

        if not available:
            self._log_tool("get_chart_screenshots", start, f"no screenshots for {requested}")
            return {"success": False, "reason": f"No screenshots available for {requested}"}

        parts = [f"{tf}({kb}KB)" for tf, kb in available.items()]
        self._log_tool("get_chart_screenshots", start, f"returning {' '.join(parts)}")

        result = {"success": True, "timeframes": list(available.keys())}
        for tf in available:
            result[tf.lower()] = True
        result["note"] = f"Charts attached: {', '.join(available.keys())}. Analyze candle patterns, S/R zone interactions, volume bars, and momentum visually."
        return result

    # ================================================================
    # PENDING ORDERS (FLO-263)
    # ================================================================

    def place_pending_order(self, order_type: str, price: float, sl: float, tp: float,
                            expiry_minutes: int = 60, reason: str = "") -> Dict[str, Any]:
        """Place a pending order (BUY_LIMIT/SELL_LIMIT/BUY_STOP/SELL_STOP)."""
        start = time.time()
        import config

        if not getattr(config, "PENDING_ORDERS_ENABLED", False):
            self._log_tool("place_pending_order", start, "DISABLED")
            return {"success": False, "reason": "Pending orders disabled"}

        valid = ("BUY_LIMIT", "SELL_LIMIT", "BUY_STOP", "SELL_STOP")
        ot = str(order_type or "").upper().strip()
        if ot not in valid:
            self._log_tool("place_pending_order", start, f"invalid type: {ot}")
            return {"success": False, "reason": f"Invalid type. Use: {', '.join(valid)}"}

        try:
            price_f, sl_f, tp_f = float(price), float(sl), float(tp)
        except Exception:
            return {"success": False, "reason": "Invalid price/sl/tp"}

        dir_s = "BUY" if "BUY" in ot else "SELL"
        sl_pips = abs(price_f - sl_f) / 0.1

        # Safety checks (same as execute_trade)
        acct = self._executor.get_account_info() or {}
        balance = self._safe_float(acct.get("balance"))
        if not balance or balance <= 0:
            self._log_tool("place_pending_order", start, "REJECTED | account balance unavailable")
            return {"success": False, "reason": "account balance unavailable"}

        try:
            open_positions_list = self._executor.get_open_positions() or []
        except Exception:
            open_positions_list = []

        is_safe, reasons = self._safety.is_safe_to_trade(
            account_balance=float(balance),
            open_positions=len(open_positions_list),
            mt5_connected=True,
            has_high_impact_news=False,
            trade_direction=dir_s,
            open_positions_list=open_positions_list,
        )
        if not is_safe:
            self._log_tool("place_pending_order", start, f"REJECTED | safety: {'; '.join(reasons[:3])}")
            return {"success": False, "reason": "; ".join(reasons[:3])}

        # Risk sizing
        risk_pct = float(getattr(config, "RISK_PER_TRADE", 2.0))
        pos = self._risk.calculate_position_size(
            account_balance=float(balance),
            risk_percent=risk_pct,
            stop_loss_pips=float(sl_pips),
        )

        exp = max(1, int(expiry_minutes)) if expiry_minutes else 60
        res = self._executor.place_pending_order(
            order_type_str=ot,
            price=price_f,
            lot_size=float(pos.lot_size),
            stop_loss=sl_f,
            take_profit=tp_f,
            expiry_minutes=exp,
            comment=f"Pending-{ot}",
        )

        if res.get("success"):
            ticket = res.get("ticket")

            # FLO-269: Record pending order in trades table (ticket=0 placeholder).
            # monitor.update_trade_open_price() updates to real ticket on fill.
            try:
                from db_writer import record_trade_open
                record_trade_open(
                    ticket=0,
                    direction=dir_s,
                    volume=float(pos.lot_size),
                    open_price=price_f,
                    sl=sl_f,
                    tp=tp_f,
                    comment=f"Pending-{ot}",
                    decision_source="floki_agent",
                )
            except Exception:
                pass

            self._log_tool("place_pending_order", start,
                f"{ot} @ {price_f} SL={sl_f} TP={tp_f} lot={pos.lot_size} exp={exp}min ticket={ticket}")
            return {"success": True, "ticket": ticket, "type": ot, "price": price_f,
                    "sl": sl_f, "tp": tp_f, "volume": float(pos.lot_size), "expiry_minutes": exp}
        else:
            self._log_tool("place_pending_order", start, f"FAILED | {res.get('error')}")
            return {"success": False, "reason": res.get("error", "placement failed")}

    def cancel_pending_order(self, ticket: int = None, cancel_all: bool = False) -> Dict[str, Any]:
        """Cancel a pending order by ticket, or cancel all pending orders."""
        start = time.time()
        if cancel_all:
            res = self._executor.cancel_all_pending()
            self._log_tool("cancel_pending_order", start, f"cancel_all | cancelled={res.get('cancelled', 0)}")
            return res
        if not ticket:
            return {"success": False, "reason": "ticket required (or cancel_all=true)"}
        res = self._executor.cancel_pending_order(int(ticket))
        self._log_tool("cancel_pending_order", start, f"ticket={ticket} | success={res.get('success')}")
        return res

    def get_pending_orders(self) -> Dict[str, Any]:
        """List all current pending orders."""
        start = time.time()
        orders = self._executor.get_pending_orders()
        self._log_tool("get_pending_orders", start, f"count={len(orders)}")
        return {"success": True, "orders": orders, "count": len(orders)}

    def get_oracle_verdict(self) -> Dict[str, Any]:
        """FLO-239: Return the latest Research Manager verdict from the Rex Bull vs Bear debate."""
        start = time.time()
        try:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "oracle_verdict.json")
            if not os.path.exists(path):
                self._log_tool("get_oracle_verdict", start, "no verdict available")
                return {"available": False, "reason": "no verdict yet"}
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            try:
                from luna_analyst import load_luna_brief
                lb = load_luna_brief()
                if lb:
                    data["luna_bias"] = lb.get("directional_bias")
                    data["luna_environment"] = lb.get("environment")
            except Exception:
                pass
            try:
                from deep_search import load_deep_research
                dr = load_deep_research()
                if dr:
                    data["analyst_consensus"] = dr.get("analyst_consensus")
            except Exception:
                pass
            self._log_tool("get_oracle_verdict", start, f"winner={data.get('winner')} conv={data.get('conviction')}")
            return data
        except Exception as e:
            self._log_tool("get_oracle_verdict", start, f"error={e}")
            return {"available": False, "reason": str(e)}

    def write_trading_journal(self, entry: str, category: str = "reflection") -> Dict[str, Any]:
        """Append an entry to Floki's persistent trading journal."""
        start = time.time()
        try:
            entry_s = str(entry or "").strip()
            if not entry_s:
                return {"success": False, "reason": "empty entry"}

            cat_s = str(category or "reflection").strip().lower()
            valid_cats = ("reflection", "missing_data", "lesson", "frustration", "idea", "market_observation")
            if cat_s not in valid_cats:
                cat_s = "reflection"

            base_dir = os.path.dirname(os.path.abspath(__file__))
            journal_path = os.path.join(base_dir, "data", "floki_journal.json")
            os.makedirs(os.path.dirname(journal_path), exist_ok=True)

            entries: list = []
            if os.path.exists(journal_path):
                try:
                    with open(journal_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, list):
                        entries = data
                except Exception:
                    entries = []

            entries.append({
                "timestamp": utc_iso(),  # FLO-309
                "category": cat_s,
                "entry": entry_s,
            })

            tmp_path = journal_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(entries, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, journal_path)

            self._log_tool("write_trading_journal", start, f"cat={cat_s} len={len(entry_s)}")
            return {"success": True, "total_entries": len(entries)}
        except Exception as e:
            self._log_tool("write_trading_journal", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    def _agent_monitor_events_path(self) -> str:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        data_dir = os.path.join(base_dir, "data")
        os.makedirs(data_dir, exist_ok=True)
        return os.path.join(data_dir, "agent_monitor_events.json")

    def _load_watch_conditions(self) -> Dict[str, Any]:
        path = self._watch_conditions_path()
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
