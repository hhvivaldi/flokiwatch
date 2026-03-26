import json
import os
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, List, Tuple

from logger import log


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
            return {"success": True, **payload}
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
                spread = ask - bid

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
            last_ts = getattr(self, "_rex_debate_last_ts", None)
            if last_ts is None or (now - float(last_ts)) > 300:
                setattr(self, "_rex_debate_turns", 0)
                setattr(self, "_rex_debate_history", [])
            setattr(self, "_rex_debate_last_ts", now)

            turns = int(getattr(self, "_rex_debate_turns", 0) or 0)
            if turns >= 5:
                return {"success": False, "reason": "debate_turn_limit"}
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

                rex = validate_with_rex(payload, timeout_seconds=20)
            except Exception as e:
                self._log_tool("debate_with_rex", start, f"error={e}")
                return {"success": False, "reason": "rex_unavailable"}

            if not isinstance(rex, dict) or not rex.get("success"):
                _rex_reason = rex.get("reason") if isinstance(rex, dict) else "rex_failed"
                log.warning(f"REX | debate_with_rex failed: {_rex_reason}")
                self._log_tool("debate_with_rex", start, f"failed={_rex_reason}")
                return {"success": False, "reason": _rex_reason}

            agree = rex.get("agree")
            reasoning = str(rex.get("reasoning") or "").strip()
            concerns = rex.get("concerns") if isinstance(rex.get("concerns"), list) else []
            suggested_adjustment = str(rex.get("suggested_adjustment") or "").strip()

            try:
                from db_writer import record_agent_event

                floki_text = str(my_reasoning or "").strip()
                if dir_s:
                    floki_text = (f"{dir_s}: " + floki_text).strip()
                if floki_text:
                    record_agent_event(
                        "DEBATE",
                        floki_text[:4000],
                        payload={"turn": turns},
                        author="FLOKI",
                    )

                rex_text = str(reasoning or "").strip()
                if rex_text:
                    record_agent_event(
                        "DEBATE",
                        rex_text[:4000],
                        payload={"turn": turns, "agree": bool(agree), "data_verified": True},
                        author="REX",
                    )

                # FLO-78: Discord card for Rex debate
                try:
                    from discord_cards import build_rex_debate_card, send_built_card
                    send_built_card(build_rex_debate_card(
                        floki_wants=f"{dir_s} {int(round(conf_f))}% conf",
                        rex_says=rex_text[:200],
                        agree=bool(agree),
                        data_verified=True,
                        suggestion=suggested_adjustment or None,
                    ))
                except Exception:
                    pass
            except Exception:
                pass

            try:
                history.append(
                    {
                        "turn": turns,
                        "floki": str(my_reasoning or "").strip(),
                        "rex": reasoning,
                        "agree": bool(agree),
                    }
                )
                setattr(self, "_rex_debate_history", history[-10:])
            except Exception:
                pass

            # FLO-64: Log data snapshot Rex received
            _rsi_val = (indicators or {}).get("rsi")
            _luna_env = (luna_context or {}).get("environment", "N/A")
            _luna_risk = (luna_context or {}).get("risk_level", "N/A")
            _price_mid = price.get("mid") if isinstance(price, dict) else None
            log.info(
                f"REX | Debate with data snapshot — price {_price_mid or 'N/A'}, RSI {_rsi_val or 'N/A'}, Luna {_luna_env} risk {_luna_risk}"
            )
            log.info(
                f"DEBATE | turn={turns}/5 | Floki: {dir_s} conf:{int(round(conf_f))}% | Rex: {'AGREE' if agree else 'DISAGREE'} — {reasoning[:140]}"
            )

            self._log_tool("debate_with_rex", start, f"turn={turns} agree={agree}")
            return {
                "success": True,
                "turn": turns,
                "agree": agree,
                "reasoning": reasoning,
                "concerns": concerns,
                "suggested_adjustment": suggested_adjustment,
            }
        except Exception as e:
            self._log_tool("debate_with_rex", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    # ---------------------------------------------------------------------
    # Position management tools
    # ---------------------------------------------------------------------

    def set_watch_conditions(self, ticket: int, conditions: List[Dict[str, Any]]) -> Dict[str, Any]:
        start = time.time()
        try:
            try:
                t = int(ticket)
            except Exception:
                return {"success": False, "reason": "invalid ticket"}

            if not isinstance(conditions, list) or not conditions:
                return {"success": False, "reason": "conditions must be a non-empty list"}

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
                "updated_at": datetime.utcnow().isoformat(),
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

            now_iso = datetime.utcnow().isoformat()
            payload = {
                "updated_at": now_iso,
                "sleep_started_at": now_iso,
                "max_sleep_minutes": msm,
                "conditions": cleaned,
            }

            ok = self._write_json_atomic(self._wake_conditions_path(), payload)
            if not ok:
                return {"success": False, "reason": "persist failed"}

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
            if tf not in ("M5", "H1", "H4", "D1"):
                self._log_fail("get_candles", start, "unsupported timeframe")
                return {"success": False, "reason": "unsupported timeframe"}

            try:
                c = int(count)
            except Exception:
                c = 0
            if c <= 0:
                self._log_fail("get_candles", start, "count must be positive")
                return {"success": False, "reason": "count must be positive"}
            c = min(c, 50)

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

            self._log_tool("get_indicators", start)
            return out
        except Exception as e:
            self._log_tool("get_indicators", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    def get_sr_zones(self) -> Dict[str, Any]:
        start = time.time()
        try:
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

            open_positions_list = []
            try:
                open_positions_list = self._executor.get_open_positions() or []
            except Exception:
                open_positions_list = []

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
                return {"success": False, "reason": str(reason)}

            fill_price = self._safe_float(getattr(res, "price", None))
            ticket = getattr(res, "ticket", None)

            # FLO-114: Guard against phantom trades — ticket must be a real positive int
            if not ticket or (isinstance(ticket, (int, float)) and int(ticket) <= 0):
                reason = getattr(res, "error_message", None) or "ticket_not_resolved"
                self._log_tool("execute_trade", start, f"{dir_s} | REJECTED | ticket={ticket} ({reason})")
                return {"success": False, "reason": str(reason)}

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

    def adjust_trade(self, ticket: int, new_sl: float, new_tp: float) -> Dict[str, Any]:
        start = time.time()
        try:
            try:
                t = int(ticket)
            except Exception:
                return {"success": False, "reason": "invalid ticket"}

            sl_f = self._safe_float(new_sl)
            tp_f = self._safe_float(new_tp)
            if sl_f is None and tp_f is None:
                return {"success": False, "reason": "invalid new sl/tp"}

            if tp_f is None:
                log.warning("ADJUST_TRADE | BLOCKED | reason=tp_missing")
                self._log_tool("adjust_trade", start, f"ticket={t} | blocked | reason=tp_missing")
                return {"success": False, "reason": "tp_missing"}

            pos = self._get_position_by_ticket(t)
            if pos is None:
                log.warning("ADJUST_TRADE | BLOCKED | reason=position_not_found")
                self._log_tool("adjust_trade", start, f"ticket={t} | blocked | reason=position_not_found")
                return {"success": False, "reason": "position_not_found"}

            direction = str(getattr(pos, "direction", "") or "").strip().upper()
            current_price = self._safe_float(getattr(pos, "current_price", None))
            old_sl = self._safe_float(getattr(pos, "sl", None))
            old_tp = self._safe_float(getattr(pos, "tp", None))
            if current_price is None or direction not in ("BUY", "SELL"):
                log.warning("ADJUST_TRADE | BLOCKED | reason=invalid_position_data")
                self._log_tool("adjust_trade", start, f"ticket={t} | blocked | reason=invalid_position_data")
                return {"success": False, "reason": "invalid_position_data"}

            if self._is_sl_widening(direction, current_price, old_sl, sl_f):
                log.warning("ADJUST_TRADE | BLOCKED | reason=sl_widening")
                self._log_tool("adjust_trade", start, f"ticket={t} | blocked | reason=sl_widening")
                return {"success": False, "reason": "sl_widening"}

            state, successful_adjustments, now = self._get_adjust_trade_window(t)
            if len(successful_adjustments) >= int(MAX_ADJUSTMENTS_PER_HOUR):
                log.warning("ADJUST_TRADE | BLOCKED | reason=rate_limit")
                self._log_tool("adjust_trade", start, f"ticket={t} | blocked | reason=rate_limit count={len(successful_adjustments)}")
                return {"success": False, "reason": "rate_limit"}

            res = self._executor.modify_position(t, new_sl=sl_f, new_tp=tp_f)
            if not getattr(res, "success", False):
                reason = getattr(res, "error_message", None) or "adjust failed"
                self._log_tool("adjust_trade", start, f"ticket={t} | success=false | {reason}")
                return {"success": False, "reason": str(reason)}

            if not self._record_adjust_trade_success(state, t, successful_adjustments, now=now):
                self._log_tool("adjust_trade", start, f"ticket={t} | success=false | rate_limit_persist_failed")
                return {"success": False, "reason": "rate_limit_persist_failed"}

            log.info(
                f"ADJUST_TRADE | SL: {self._format_adjust_value(old_sl)}→{self._format_adjust_value(sl_f)} | "
                f"TP: {self._format_adjust_value(old_tp)}→{self._format_adjust_value(tp_f)} | reason=agent_adjust"
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
                now = datetime.now()
                today = now.date().isoformat()
                if str(payload.get("session_date") or "") != today:
                    payload["session_date"] = today
                    payload["notes"] = []
                    payload["last_updated"] = now.isoformat(timespec="seconds")
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

            now = datetime.now()
            today = now.date().isoformat()

            payload: Dict[str, Any] = {
                "session_date": today,
                "thesis": thesis_s,
                "trades_today": 0,
                "wins_today": 0,
                "losses_today": 0,
                "notes": [],
                "last_updated": now.isoformat(timespec="seconds"),
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
                    "last_updated": now.isoformat(timespec="seconds"),
                }

            if thesis_s:
                payload["thesis"] = thesis_s

            if not isinstance(payload.get("notes"), list):
                payload["notes"] = []

            if note_s:
                payload["notes"].append({"time": now.strftime("%H:%M"), "note": note_s})

                # Keep max 20 notes, protect Sage notes from truncation.
                # Strategy: keep all notes where source == 'sage', truncate only non-sage notes to last 19.
                try:
                    all_notes = payload.get("notes") or []
                    sage_notes = []
                    normal_notes = []
                    for n in all_notes:
                        if isinstance(n, dict) and str(n.get("source") or "").strip().lower() == "sage":
                            sage_notes.append(n)
                        else:
                            normal_notes.append(n)
                    normal_notes = normal_notes[-19:]
                    payload["notes"] = normal_notes + sage_notes
                    payload["notes"] = payload["notes"][-20:]
                except Exception:
                    payload["notes"] = payload["notes"][-20:]

            payload["last_updated"] = now.isoformat(timespec="seconds")

            try:
                with open(mem_path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
            except Exception:
                self._log_tool("write_session_memory", start, "error=write_failed")
                return {"success": False, "reason": "write failed"}

            self._log_tool("write_session_memory", start, f"notes_count={len(payload.get('notes') or [])}")
            return {"success": True, "notes_count": len(payload.get("notes") or [])}
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
                "timestamp": datetime.utcnow().isoformat() + "Z",
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
