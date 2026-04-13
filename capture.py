"""
Capture-rate computation — FLO-290.

Single source of truth for "capture %" = what fraction of the maximum
favorable excursion (MFE) the closed trade actually captured.

Formula is PIPS / PIPS (unit-consistent). The previous DOLLARS / PIPS
formula was a unit-mismatch bug that understated capture ~10x for gold
(e.g. a trade with final +194.6 pips / MFE 244.3 pips = 79.7% capture
was reported as 15.9% because $38.92 was divided into 244.3).
"""
from typing import Optional

PIP_SIZE = 0.1  # XAU/USD


def compute_capture_pct(
    direction: Optional[str],
    entry_price: Optional[float],
    close_price: Optional[float],
    mfe_points: Optional[float],
) -> Optional[float]:
    """Return capture percentage (pips captured / pips at MFE peak × 100).

    Returns:
      - A float (the capture %) when all inputs are valid and MFE > 0
      - 0.0 when MFE <= 0 (trade was never in profit — nothing to capture)
      - None when inputs are missing or malformed

    Contract note: MFE is the maximum favorable excursion in pips (signed).
    If the trade never went above entry (BUY) or below entry (SELL), MFE
    can be <= 0 — capture is trivially 0%.
    """
    if (
        direction is None
        or entry_price is None
        or close_price is None
        or mfe_points is None
    ):
        return None
    try:
        mfe = float(mfe_points)
        entry = float(entry_price)
        close = float(close_price)
    except (TypeError, ValueError):
        return None

    if mfe <= 0:
        return 0.0

    direction = str(direction).upper()
    if direction == "BUY":
        pnl_pips = (close - entry) / PIP_SIZE
    elif direction == "SELL":
        pnl_pips = (entry - close) / PIP_SIZE
    else:
        return None

    try:
        return round((pnl_pips / mfe) * 100, 1)
    except ZeroDivisionError:
        return 0.0
