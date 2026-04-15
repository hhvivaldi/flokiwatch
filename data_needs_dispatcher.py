"""
FLO-302 step 4 — data_needs dispatcher.

Filters Floki's structured self-assessment and routes it to the Discord
#data_needs channel via alerts.alert_data_needs(). Tracks per-item drift
across recent cycles so repeated complaints without action fire a
warning.

Filter rule:
  Send the standard embed only when at least one of {missing_data,
  biggest_obstacle, suggestions, tool_errors} is non-empty. "No missing
  data" cycles produce no Discord noise — but they STILL update drift
  history, so a run of empty cycles correctly breaks a drift streak.

Drift rule:
  Track normalized missing_data signatures for the last N cycles (default
  5). If the same item appears in the current cycle AND in at least K-1
  prior cycles (K=3 by default), emit a drift warning alongside the
  standard embed.

State lives at module scope — Python's import cache gives us a single
instance for the whole bot process. State resets on bot restart by design:
drift is a sustained-behavior signal, and a fresh bot should earn its
drift alerts from its own cycles.
"""
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Set

from logger import log


_HISTORY_DEPTH = 5
_DRIFT_THRESHOLD = 3  # current cycle + >=2 prior = 3 in a row

# FLO-306: track BOTH not_called and unavailable independently. Drift on
# not_called = chronic skip (Floki avoiding a tool he could use). Drift on
# unavailable = persistent broken/missing data (real infra issue). Each
# carries different remediation, so we surface them with separate labels.
# FLO-315: also track feature_requests — repeated asks for the same
# capability = genuine need we should consider building.
_recent_not_called: Deque[Set[str]] = deque(maxlen=_HISTORY_DEPTH)
_recent_unavailable: Deque[Set[str]] = deque(maxlen=_HISTORY_DEPTH)
_recent_feature_requests: Deque[Set[str]] = deque(maxlen=_HISTORY_DEPTH)
_recent_timestamps: Deque[str] = deque(maxlen=_HISTORY_DEPTH)


def _normalize(items: Any) -> List[str]:
    """Lowercase + strip each list item, drop empties, dedup."""
    if not items:
        return []
    try:
        return sorted({str(x).strip().lower() for x in items if str(x).strip()})
    except Exception:
        return []


def _check_drift_in(
    current_items: Any,
    history: Deque[Set[str]],
    current_ts: str,
) -> List[Dict[str, Any]]:
    """Return items appearing in the current cycle AND in a contiguous run
    of >=K-1 immediately-prior cycles within the supplied history deque.

    "Consecutive" is the key word: any intervening cycle without the item
    breaks the streak. Walks history newest→oldest and stops at first miss.

    Must be called BEFORE pushing the current cycle's signature to history.
    """
    current_set = set(_normalize(current_items))
    if not current_set:
        return []

    drifting: List[Dict[str, Any]] = []
    for item in current_set:
        count = 1  # the current cycle
        first_seen = current_ts
        for prev_sig, prev_ts in zip(reversed(history), reversed(_recent_timestamps)):
            if item in prev_sig:
                count += 1
                first_seen = prev_ts
            else:
                break
        if count >= _DRIFT_THRESHOLD:
            drifting.append({"item": item, "count": count, "first_seen": first_seen})
    return drifting


def dispatch_data_needs(
    data_needs: Optional[Dict[str, Any]],
    *,
    decision: Optional[str],
    confidence: Optional[int],
    ticket_summary: Optional[str],
    timestamp_utc: str,
) -> bool:
    """Filter + send. Returns True if a Discord message was dispatched.

    Always advances drift history (even on "no signal" cycles) so that
    consecutive-cycle counting is accurate.
    """
    if not isinstance(data_needs, dict):
        return False

    # FLO-306: split into not_called (skipped) and unavailable (broken/stale).
    # Back-compat: legacy "missing_data" still present in old payloads → treat
    # as not_called (matches its observed semantics under FLO-302 schema).
    not_called = data_needs.get("not_called")
    if not not_called:
        not_called = data_needs.get("missing_data") or []
    unavailable = data_needs.get("unavailable") or []
    obstacle = (data_needs.get("biggest_obstacle") or "").strip()
    tool_errors = data_needs.get("tool_errors") or []
    # FLO-315: suggestions split. feature_requests participates in dispatch
    # filter + drift detection; self_critique does NOT trigger dispatch alone
    # (it fires nearly every cycle and would re-create the FLO-302 noise
    # problem). Legacy "suggestions" key routes into feature_requests.
    feature_requests = data_needs.get("feature_requests")
    if not feature_requests:
        feature_requests = data_needs.get("suggestions") or []

    # 1. Drift checks BEFORE appending current cycle (one per tracked field).
    drift_not_called = _check_drift_in(not_called, _recent_not_called, timestamp_utc)
    drift_unavailable = _check_drift_in(unavailable, _recent_unavailable, timestamp_utc)
    drift_feature_requests = _check_drift_in(
        feature_requests, _recent_feature_requests, timestamp_utc
    )

    # 2. Advance history (every cycle, even no-signal ones, so streaks break).
    _recent_not_called.append(set(_normalize(not_called)))
    _recent_unavailable.append(set(_normalize(unavailable)))
    _recent_feature_requests.append(set(_normalize(feature_requests)))
    _recent_timestamps.append(timestamp_utc)

    # 3. Filter: skip silently when no concrete items in any dispatch-worthy
    # field. self_critique is intentionally excluded — it's every-cycle noise.
    has_signal = bool(
        not_called or unavailable or obstacle or feature_requests or tool_errors
    )
    if not has_signal:
        return False

    # 4. FLO-305: assemble boss_notes summary for Hermano's Discord view.
    # "acked" = acknowledged IN THIS CYCLE only (sourced from boss_notes module
    # state, because dismissed notes are removed from the JSON on ack and can't
    # be reconstructed from file state alone).
    # "pending" = currently-active notes that still require ack.
    boss_summary: Optional[Dict[str, List[Dict[str, str]]]] = None
    try:
        from boss_notes import pop_last_cycle_acks, get_active_notes
        acks = pop_last_cycle_acks()
        active = get_active_notes()
        # Build id → short_text lookup from ACTIVE notes (dismissed ones are gone;
        # we display just their ids for the "acked" list in that case).
        id_to_text = {
            str(n.get("id")): str(n.get("text") or "")[:40]
            for n in active
        }
        def _labels(ids: List[str]) -> List[Dict[str, str]]:
            out = []
            for i in ids:
                out.append({"id": i, "text": id_to_text.get(i, "")})
            return out
        acked_ids = list(acks.get("acked", [])) + list(acks.get("dismissed", []))
        pending_ids = [
            str(n.get("id")) for n in active
            if n.get("requires_ack", True) and not n.get("acknowledged_at")
        ]
        if acked_ids or pending_ids:
            boss_summary = {
                "acked":   _labels(acked_ids),
                "pending": _labels(pending_ids),
            }
    except Exception as e:
        log.debug(f"[dispatch_data_needs] boss_notes summary skipped: {e}")
        boss_summary = None

    # 5. Send. drift_by_field tags each drift list with its source field so the
    # Discord embed can label them clearly ("not_called drift" vs "unavailable").
    drift_by_field = {}
    if drift_not_called:
        drift_by_field["not_called"] = drift_not_called
    if drift_unavailable:
        drift_by_field["unavailable"] = drift_unavailable
    if drift_feature_requests:
        drift_by_field["feature_requests"] = drift_feature_requests  # FLO-315

    try:
        from alerts import alert_data_needs
        return alert_data_needs(
            timestamp_utc=timestamp_utc,
            decision=decision,
            confidence=confidence,
            data_needs=data_needs,
            ticket_summary=ticket_summary,
            drift=drift_by_field or None,
            boss_notes_summary=boss_summary,
        )
    except Exception as e:
        log.debug(f"[dispatch_data_needs] send failed (ignored): {e}")
        return False


def reset_state() -> None:
    """Test hook — clears drift history."""
    _recent_not_called.clear()
    _recent_unavailable.clear()
    _recent_feature_requests.clear()  # FLO-315
    _recent_timestamps.clear()
