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

_recent_signatures: Deque[Set[str]] = deque(maxlen=_HISTORY_DEPTH)
_recent_timestamps: Deque[str] = deque(maxlen=_HISTORY_DEPTH)


def _normalize(items: Any) -> List[str]:
    """Lowercase + strip each list item, drop empties, dedup."""
    if not items:
        return []
    try:
        return sorted({str(x).strip().lower() for x in items if str(x).strip()})
    except Exception:
        return []


def _check_drift(current_missing: Any, current_ts: str) -> List[Dict[str, Any]]:
    """Return items appearing in the current cycle AND in a contiguous run
    of >=K-1 immediately-prior cycles.

    "Consecutive" is the key word: any intervening cycle without the item
    breaks the streak. Walks history newest→oldest and stops at first miss.

    Must be called BEFORE pushing the current cycle's signature to history,
    otherwise the current cycle would count twice.
    """
    current_set = set(_normalize(current_missing))
    if not current_set:
        return []

    drifting: List[Dict[str, Any]] = []
    for item in current_set:
        count = 1  # the current cycle
        first_seen = current_ts
        # Walk priors newest→oldest; stop at the first cycle missing this item.
        for prev_sig, prev_ts in zip(
            reversed(_recent_signatures), reversed(_recent_timestamps)
        ):
            if item in prev_sig:
                count += 1
                first_seen = prev_ts  # the earliest in the contiguous run so far
            else:
                break  # streak broken
        if count >= _DRIFT_THRESHOLD:
            drifting.append({
                "item": item,
                "count": count,
                "first_seen": first_seen,
            })
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

    missing = data_needs.get("missing_data") or []
    obstacle = (data_needs.get("biggest_obstacle") or "").strip()
    suggestions = data_needs.get("suggestions") or []
    tool_errors = data_needs.get("tool_errors") or []

    # 1. Drift check BEFORE appending the current cycle.
    drift = _check_drift(missing, timestamp_utc)

    # 2. Advance history — every cycle, including no-signal ones.
    _recent_signatures.append(set(_normalize(missing)))
    _recent_timestamps.append(timestamp_utc)

    # 3. Filter: skip silently when Floki reports no concrete items.
    has_signal = bool(missing or obstacle or suggestions or tool_errors)
    if not has_signal:
        return False

    # 4. Send.
    try:
        from alerts import alert_data_needs
        return alert_data_needs(
            timestamp_utc=timestamp_utc,
            decision=decision,
            confidence=confidence,
            data_needs=data_needs,
            ticket_summary=ticket_summary,
            drift=drift or None,
        )
    except Exception as e:
        log.debug(f"[dispatch_data_needs] send failed (ignored): {e}")
        return False


def reset_state() -> None:
    """Test hook — clears drift history."""
    _recent_signatures.clear()
    _recent_timestamps.clear()
