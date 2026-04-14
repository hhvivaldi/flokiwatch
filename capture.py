"""
Capture-rate computation — FLO-290.

Single source of truth for "capture %" = what fraction of the maximum
favorable excursion (MFE) the closed trade actually captured.

Formula is PIPS / PIPS (unit-consistent). The previous DOLLARS / PIPS
formula was a unit-mismatch bug that understated capture ~10x for gold
(e.g. a trade with final +194.6 pips / MFE 244.3 pips = 79.7% capture
was reported as 15.9% because $38.92 was divided into 244.3).

FLO-300: display helper. Raw capture can explode to huge negatives when
MFE is tiny and the trade lost big (e.g. MFE=+1.1 pips, final=-67.1 pips
→ raw = -6100%). That's correct math but useless UX. The display helper
clamps and substitutes "LOSS" for near-zero-MFE losing trades.
"""
from typing import Optional

PIP_SIZE = 0.1  # XAU/USD
_NOISE_FLOOR_PIPS = 10.0            # FLO-300: MFE below this is "noise" for losing trades
_DISPLAY_MIN = -100.0               # clamp floor for display
_DISPLAY_MAX = 500.0                # clamp ceiling for display


def pnl_pips(direction: Optional[str], entry_price: Optional[float],
             close_price: Optional[float]) -> Optional[float]:
    """Compute realized P&L in pips for a closed trade. Sign: + = winner."""
    if not direction or entry_price is None or close_price is None:
        return None
    try:
        d = str(direction).upper()
        entry = float(entry_price); close = float(close_price)
    except (TypeError, ValueError):
        return None
    if d == "BUY":
        return (close - entry) / PIP_SIZE
    if d == "SELL":
        return (entry - close) / PIP_SIZE
    return None


def format_capture_display(
    raw_capture_pct: Optional[float],
    mfe_points: Optional[float],
    pnl_pips_value: Optional[float],
) -> str:
    """FLO-300: human-readable capture for UI/XML.

    - None / missing inputs → "—"
    - Losing trade (pnl < 0) with MFE below noise floor → "LOSS"
      (e.g. MFE=+1.1p, pnl=-67p → would be -6100%; show "LOSS")
    - Raw beyond display band → clamped ("-100%" / "500%")
    - Integer values formatted without trailing ".0"
    Raw numeric value is NOT modified — only the rendered string.
    """
    if raw_capture_pct is None:
        return "—"
    try:
        raw = float(raw_capture_pct)
    except (TypeError, ValueError):
        return "—"
    # Noise-floor substitution: losing trade that never got meaningful favorable
    # excursion. "LOSS" is more informative than a huge negative percentage.
    if (
        pnl_pips_value is not None
        and mfe_points is not None
        and pnl_pips_value < 0
        and abs(mfe_points) < _NOISE_FLOOR_PIPS
    ):
        return "LOSS"
    # Clamp for display
    clamped = max(_DISPLAY_MIN, min(_DISPLAY_MAX, raw))
    sign = "+" if clamped > 0 else ""
    if float(clamped).is_integer():
        return f"{sign}{int(clamped)}%"
    return f"{sign}{clamped:.1f}%"


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
