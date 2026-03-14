import json
import os
import time
from datetime import datetime
from typing import Any, Dict, Optional, List

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

    # ---------------------------------------------------------------------
    # Market data tools (cache-only)
    # ---------------------------------------------------------------------

    def get_current_price(self) -> Dict[str, Any]:
        start = time.time()
        try:
            dp = self._last_agent_data()
            if not dp:
                return self._no_cache()

            out = self._extract_price_from_cache(dp)
            if not out:
                return self._no_cache()

            self._log_tool("get_current_price", start, f"bid={out.get('bid')} ask={out.get('ask')}")
            return out
        except Exception as e:
            self._log_tool("get_current_price", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    def get_candles(self, timeframe: str, count: int) -> Dict[str, Any]:
        start = time.time()
        try:
            tf = str(timeframe or "").upper().strip()
            if tf not in ("M5", "H1", "H4", "D1"):
                return {"success": False, "reason": "unsupported timeframe"}

            try:
                c = int(count)
            except Exception:
                c = 0
            if c <= 0:
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
                    return self._no_cache()
                try:
                    # Expect columns: time, open, high, low, close, tick_volume/volume
                    cols = set(getattr(df, "columns", []))
                    required = {"open", "high", "low", "close"}
                    if not required.issubset(cols):
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
                    return {"success": False, "reason": "failed to build candles from cache"}

            if candles is None:
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
                return self._no_cache()

            ind = dp.get("indicators")
            if not isinstance(ind, dict) or not ind:
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
                out["ema200"] = self._safe_float(emas.get("ema200"))
            except Exception:
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
                return self._no_cache()

            sr = dp.get("sr_zones") or dp.get("support_resistance")
            if isinstance(sr, dict) and "zones" in sr:
                zones = sr.get("zones")
            else:
                zones = sr

            if not isinstance(zones, list) or not zones:
                return self._no_cache()

            self._log_tool("get_sr_zones", start, f"zones={len(zones)}")
            return {"zones": zones}
        except Exception as e:
            self._log_tool("get_sr_zones", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

    def get_fibonacci_levels(self) -> Dict[str, Any]:
        start = time.time()
        try:
            dp = self._last_agent_data()
            if not dp:
                return self._no_cache()

            fib = dp.get("fibonacci") or dp.get("fib")
            if not isinstance(fib, dict) or not fib:
                return self._no_cache()

            levels = fib.get("levels") if isinstance(fib.get("levels"), dict) else fib.get("levels")
            if not isinstance(levels, dict) or not levels:
                return self._no_cache()

            out = {
                "levels": levels,
                "swing_high": fib.get("swing_high"),
                "swing_low": fib.get("swing_low"),
            }
            self._log_tool("get_fibonacci_levels", start)
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
                return self._no_cache()

            news = dp.get("headlines") or dp.get("news") or dp.get("news_headlines")
            if isinstance(news, dict) and "headlines" in news:
                headlines = news.get("headlines")
            else:
                headlines = news

            if not isinstance(headlines, list):
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
                return self._no_cache()

            macro = dp.get("macro")
            if not isinstance(macro, dict) or not macro:
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
                return self._no_cache()

            cal = dp.get("calendar") or dp.get("economic_calendar")
            if not isinstance(cal, dict) or not cal:
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
                return self._no_cache()

            ml = dp.get("ml") or dp.get("ml_prediction") or dp.get("ml_predictions")
            if not isinstance(ml, dict) or not ml:
                return self._no_cache()

            self._log_tool("get_ml_prediction", start)
            return ml
        except Exception as e:
            self._log_tool("get_ml_prediction", start, f"error={e}")
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

    def execute_trade(self, direction: str, sl: float, tp: float) -> Dict[str, Any]:
        start = time.time()
        try:
            dp = self._last_agent_data()
            if not dp:
                return self._no_cache()

            price = self._extract_price_from_cache(dp)
            if not price:
                return self._no_cache()

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
                return {"success": False, "reason": "could not compute sl pips"}

            if sl_pips < 150 or sl_pips > 800:
                return {"success": False, "reason": f"sl out of range ({sl_pips:.1f} pips)"}

            # Safety checks (max positions, daily loss, market open buffers, etc.)
            acct = self._executor.get_account_info() or {}
            balance = self._safe_float(acct.get("balance"))
            if balance is None:
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

            self._log_tool(
                "execute_trade",
                start,
                f"{dir_s} @ {fill_price} | ticket={ticket} | success",
            )

            return {
                "success": True,
                "ticket": int(ticket) if ticket is not None else None,
                "fill_price": fill_price,
                "sl": float(sl_f),
                "tp": float(tp_f),
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

            res = self._executor.modify_position(t, new_sl=sl_f, new_tp=tp_f)
            if not getattr(res, "success", False):
                reason = getattr(res, "error_message", None) or "adjust failed"
                self._log_tool("adjust_trade", start, f"ticket={t} | success=false | {reason}")
                return {"success": False, "reason": str(reason)}

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

            self._log_tool("read_session_memory", start)
            return payload
        except Exception as e:
            self._log_tool("read_session_memory", start, f"error={e}")
            return {"success": False, "reason": "tool_error"}

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
                payload = {
                    "session_date": today,
                    "thesis": thesis_s,
                    "trades_today": 0,
                    "wins_today": 0,
                    "losses_today": 0,
                    "notes": [],
                    "last_updated": now.isoformat(timespec="seconds"),
                }

            if thesis_s:
                payload["thesis"] = thesis_s

            if not isinstance(payload.get("notes"), list):
                payload["notes"] = []

            if note_s:
                payload["notes"].append({"time": now.strftime("%H:%M"), "note": note_s})
                payload["notes"] = payload["notes"][-10:]

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
