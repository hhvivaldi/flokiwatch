# Tech Direction / Tech Risk Split — Design Document (v2)

## ❌ STATUS: ABANDONED (Feb 23, 2026)

**Backtest Results (6-month period: Aug 2025 → Feb 2026)**

| Metric | Baseline | Tech Direction | Delta |
|--------|----------|----------------|-------|
| Trades | 231 | 276 | +45 |
| Win Rate | 72.7% | 67.0% | **-5.7%** |
| Profit Factor | 2.24 | 1.71 | **-0.54** |
| P&L | $2,258.51 | $1,634.38 | **-$624** |

**Verdict**: The Tech Direction split WORSENS performance. PF drop of 0.54 is unacceptable.

**Root Cause**: RSI/BB penalties in the current tech_score are protective, not bugs. They suppress the score during overbought conditions, preventing bad entries. Removing them (by splitting to Tech Direction) generates more low-quality entries.

**Key Learnings**:
1. EMAs and MACD are counter-productive for BUY direction (confirmed by diagnostic)
2. RSI/BB penalties are a FEATURE, not a bug
3. Tech Score has low predictive value for direction but protects against bad entries
4. The +45 additional trades were net negative — more trades ≠ better

**Note**: Conflict threshold variants (A/B/C) were identical due to a bug — the threshold override wasn't applied. All variants used default threshold 65. This bug needs fixing for future conflict scenario testing, but given the core result (Tech Direction split fails), it's lower priority.

---

Split the Technical pillar into two components: **Tech Direction** (for score calculation) and **Tech Risk** (for confidence adjustment).

---

## Problem Statement

Current `tech_score` mixes direction indicators (EMAs, MACD) with risk indicators (RSI overbought, Bollinger extremes). Backtest analysis of 662 trades showed:

- **RSI/Bollinger penalties protect against bad entries** (RSI > 70 at entry: 28.5% winners vs 46.7% losers)
- **EMAs and MACD are counter-productive for BUY direction** (losers have HIGHER EMA/MACD scores)
- Mixing direction and risk in one number wastes the 30% weight

**Solution**: Use direction-only indicators for the 30% pillar weight, move risk indicators to confidence adjustment.

---

## Tech Direction (NEW) — Score Component

**Purpose**: Measures market direction only. Feeds into Central Brain weighted average with **30% weight**.

**Score Range**: 0-100 (100 = strong bullish, 0 = strong bearish, 50 = neutral)

### Components and Scoring

#### 1. Price vs EMAs — 30 points max

| Condition | Points |
|-----------|--------|
| Price > EMA9 | +6 |
| Price > EMA21 | +6 |
| Price > EMA50 | +6 |
| Price < EMA9 | +0 |
| Price < EMA21 | +0 |
| Price < EMA50 | +0 |
| Price between EMA (e.g., > EMA50 but < EMA21) | proportional |

**Formula**:
```python
ema_score = 0
if close > ema9:
    ema_score += 6
if close > ema21:
    ema_score += 6
if close > ema50:
    ema_score += 6
# Inverse for bearish (below all = 0, above all = 18)
# Normalize: 18 points → scale to 30
ema_score = ema_score * (30 / 18)  # Max 30
```

#### 2. EMA Alignment — 25 points max (with distance gradation)

**Base alignment** (0-15 points):
| Condition | Points |
|-----------|--------|
| EMA9 > EMA21 > EMA50 (bullish aligned) | 15 |
| EMA9 < EMA21 < EMA50 (bearish aligned) | 0 |
| Partial alignment | 7 |

**Distance bonus** (0-10 points):
Measures how "spread out" the EMAs are. Stronger trends have wider EMA separation.

```python
# Base alignment score
if ema9 > ema21 > ema50:
    base_score = 15  # Bullish
elif ema9 < ema21 < ema50:
    base_score = 0   # Bearish
else:
    base_score = 7   # Mixed

# Distance bonus (only for aligned EMAs)
if ema9 > ema21 > ema50 or ema9 < ema21 < ema50:
    # Calculate separation as % of price
    sep_9_21 = abs(ema9 - ema21) / ema21 * 100
    sep_21_50 = abs(ema21 - ema50) / ema50 * 100
    avg_separation = (sep_9_21 + sep_21_50) / 2
    
    # Scale: 0.1% = +2, 0.5% = +5, 1%+ = +10
    if avg_separation >= 1.0:
        distance_bonus = 10
    elif avg_separation >= 0.5:
        distance_bonus = 5 + (avg_separation - 0.5) * 10  # 5-10
    elif avg_separation >= 0.1:
        distance_bonus = 2 + (avg_separation - 0.1) * 7.5  # 2-5
    else:
        distance_bonus = avg_separation * 20  # 0-2
else:
    distance_bonus = 0

alignment_score = base_score + distance_bonus  # Max 25
```

#### 3. MACD Direction — 30 points max

| Condition | Points |
|-----------|--------|
| MACD > Signal AND MACD > 0 | 25 |
| MACD > Signal AND MACD < 0 | 18 |
| MACD < Signal AND MACD > 0 | 7 |
| MACD < Signal AND MACD < 0 | 0 |

**Histogram Bonus** (+5 max):
```python
if macd_hist > prev_macd_hist:
    macd_score += 5  # Momentum increasing
```

**Formula**:
```python
if macd > signal:
    if macd > 0:
        macd_score = 25
    else:
        macd_score = 18
else:
    if macd > 0:
        macd_score = 7
    else:
        macd_score = 0

# Histogram bonus (compare to PREVIOUS CLOSED candle)
if macd_hist > prev_macd_hist:
    macd_score = min(macd_score + 5, 30)
```

#### 4. Price Action Trend — 15 points max (PREVIOUS CLOSED CANDLE ONLY)

**IMPORTANT**: To avoid intra-candle noise, Price Action is calculated on the **previous closed H1 candle**, not the forming candle. EMAs and MACD are already stable (calculated on closed candles).

| Condition (previous candle) | Points |
|-----------|--------|
| Previous candle bullish (close > open) | +5 |
| Higher high than candle before it | +5 |
| Higher low than candle before it | +5 |

**Formula**:
```python
# Use candle[-2] (previous closed) vs candle[-3] (before that)
prev = candle[-2]  # Last closed candle
prev_prev = candle[-3]  # Candle before that

pa_score = 0
if prev['close'] > prev['open']:
    pa_score += 5
if prev['high'] > prev_prev['high']:
    pa_score += 5
if prev['low'] > prev_prev['low']:
    pa_score += 5
# Max 15 points
```

#### 5. Visual Context Features — REMOVED from Tech Direction

The current ±8 point visual context adjustment (candlestick patterns, consecutive candles, etc.) is **removed from Tech Direction**.

**Rationale**: Candlestick patterns are informational only (±8 cap is too small to cross thresholds). They remain visible in the Intel Feed but do not affect the score.

**What happens to the ±8 points**:
- **Dropped entirely** from score calculation
- Patterns still detected and shown in Intel Feed
- If future backtest shows pattern value, can be added to confidence instead

### Total Tech Direction Score

```python
raw_total = ema_score + alignment_score + macd_score + pa_score
# Max possible: 30 + 25 + 30 + 15 = 100
# Already normalized to 0-100
tech_direction = max(0, min(100, raw_total))
```

### What is EXCLUDED from Tech Direction

- RSI (moved to Tech Risk)
- Bollinger Bands position (moved to Tech Risk)
- Stochastic (moved to Tech Risk)
- Visual Context Features (removed — informational only in Intel Feed)

---

## Tech Risk (EXISTING logic) — Confidence Modifier

**Purpose**: Adjusts confidence based on overbought/oversold conditions. Does NOT affect the score.

**Applied in**: Step 12 (confidence calculation), alongside existing confirmations/alerts.

### Diagnostic Analysis: Penalty Impact

Before finalizing penalties, we analyzed the 358 winning BUY trades:

| Condition | Winning BUYs | Current Confidence | With Penalty | Below 35%? |
|-----------|--------------|-------------------|--------------|------------|
| RSI > 70 | 102 trades | Mean: 81.3, Min: 55.0 | -10 → Min: 45.0 | **0 trades** |
| RSI > 80 | 11 trades | — | -15 | **0 trades** |
| RSI>70 AND BB>80% | 95 trades | — | -20 combined | **0 trades** |

**Conclusion**: The proposed penalties do NOT recreate the blocking problem. Even with -20 combined penalty, no winning BUY would drop below 35% confidence. The minimum confidence after penalty would be ~45%.

However, 27 winning trades would drop below 50% confidence with -20 penalty. This is acceptable — it reduces position size, not blocks the trade.

### SELL Side Analysis (Issue 4)

| Metric | Winning SELLs (118) | Losing SELLs (66) | Delta |
|--------|---------------------|-------------------|-------|
| Avg Tech Score | 44.6 | 42.9 | +1.7 |
| Avg RSI | 49.4 | 47.5 | +1.9 |
| RSI < 30 | 1 | 1 | — |
| Avg BB Position | 47.2% | 43.8% | +3.4% |
| BB < 20% | 12 | 9 | — |
| Close > EMA9 | 39 (33%) | 16 (24%) | +9% |
| Close > EMA50 | 50 (42%) | 26 (39%) | +3% |

**SELL Findings**:
1. Tech Score has **slight positive correlation** for SELLs (+1.7 delta) — opposite of BUYs
2. RSI oversold (<30) is **extremely rare** for both winning and losing SELLs (1 each)
3. BB < 20% is slightly more common in winning SELLs (10% vs 14%)
4. EMAs: Winning SELLs are MORE likely to have price above EMAs (counter-intuitive)

**Implication**: SELL trades don't have the same RSI/BB extreme problem as BUYs. The penalties are primarily for BUY protection. SELL penalties can be lighter or removed.

### Risk Conditions and Penalties (REVISED)

#### RSI Risk

| Condition | Direction | Confidence Adjustment |
|-----------|-----------|----------------------|
| RSI > 70 | BUY signal | **-8** |
| RSI > 80 | BUY signal | **-12** |
| RSI < 30 | SELL signal | **-5** (rare, light penalty) |
| RSI < 20 | SELL signal | **-8** |

**Rationale**: Reduced from -10/-15 to -8/-12 for BUYs. SELL penalties lighter because RSI<30 is rare and not strongly correlated with losing SELLs.

**Formula**:
```python
def apply_rsi_risk(confidence, rsi, decision):
    if decision in ("BUY", "STRONG_BUY"):
        if rsi > 80:
            confidence -= 12
            alerts.append(f"RSI extreme overbought ({rsi:.0f}) - high reversal risk")
        elif rsi > 70:
            confidence -= 8
            alerts.append(f"RSI overbought ({rsi:.0f}) - caution")
    elif decision in ("SELL", "STRONG_SELL"):
        if rsi < 20:
            confidence -= 8
            alerts.append(f"RSI extreme oversold ({rsi:.0f}) - high bounce risk")
        elif rsi < 30:
            confidence -= 5
            alerts.append(f"RSI oversold ({rsi:.0f}) - caution")
    return confidence
```

#### Bollinger Risk

| Condition | Direction | Confidence Adjustment |
|-----------|-----------|----------------------|
| BB position > 80% | BUY signal | **-8** |
| BB position > 95% | BUY signal | **-12** |
| BB position < 20% | SELL signal | **-5** |
| BB position < 5% | SELL signal | **-8** |

**Formula**:
```python
def apply_bollinger_risk(confidence, bb_position, decision):
    # bb_position = (close - bb_lower) / (bb_upper - bb_lower) * 100
    if decision in ("BUY", "STRONG_BUY"):
        if bb_position > 95:
            confidence -= 12
            alerts.append(f"Price at BB extreme ({bb_position:.0f}%) - high reversal risk")
        elif bb_position > 80:
            confidence -= 8
            alerts.append(f"Price near BB upper ({bb_position:.0f}%) - caution")
    elif decision in ("SELL", "STRONG_SELL"):
        if bb_position < 5:
            confidence -= 8
            alerts.append(f"Price at BB extreme ({bb_position:.0f}%) - high bounce risk")
        elif bb_position < 20:
            confidence -= 5
            alerts.append(f"Price near BB lower ({bb_position:.0f}%) - caution")
    return confidence
```

#### Stochastic Risk — REMOVED

Stochastic is removed from Tech Risk. It adds noise without clear protective value in the backtest data.

---

## Central Brain Changes

### Step 1: Receive Data

**Current**:
```python
tech_score = tech_data.get("score", 50)
```

**New**:
```python
tech_direction = tech_data.get("direction_score", 50)  # NEW
tech_risk = tech_data.get("risk_data", {})  # RSI, BB, Stoch values
```

### Step 8: Calculate Final Score

**Current**:
```python
score += tech_score * weights["technical"]  # 30%
```

**New**:
```python
score += tech_direction * weights["technical"]  # 30% (direction only)
```

### Step 12: Calculate Confidence

**Current**: Uses confirmations/alerts from various sources.

**New**: Add Tech Risk adjustments after existing logic:
```python
# After existing confidence calculation...
confidence = apply_rsi_risk(confidence, tech_risk.get("rsi", 50), decision)
confidence = apply_bollinger_risk(confidence, tech_risk.get("bb_position", 50), decision)
```

---

## Dashboard Visibility

### Brain Pillars Card

| Pillar | Shows |
|--------|-------|
| Tech | Tech Direction score (0-100) |

No change to pillar bar display — just shows the new direction-only score.

### Intel Feed

New alerts from Tech Risk:
- "RSI overbought (75) - caution"
- "Price near BB upper (85%) - caution"
- "RSI extreme overbought (82) - high reversal risk"

These appear alongside existing alerts (MACD divergence, volume warnings, etc.).

---

## What Does NOT Change

- **ML pillar**: Untouched (25% weight)
- **Momentum pillar**: Untouched (15% weight)
- **News pillar**: Untouched (20% weight)
- **Calendar pillar**: Untouched (10% weight)
- **Pillar weights**: Tech remains 30%
- **BUY/SELL thresholds**: 65/35 (58 for conflict scenario)
- **All scenarios**: Same detection logic
- **Existing confidence modifiers**: Volume gate, MTF trend, etc.

---

## Files to Modify

1. **`technical_analyzer.py`**
   - Add `calculate_tech_direction_score()` function
   - Add `get_tech_risk_data()` function
   - Modify `analyze_technical_detailed()` to return both

2. **`central_brain.py`**
   - Step 1: Receive `tech_direction` and `tech_risk`
   - Step 8: Use `tech_direction` for weighted average
   - Step 12: Apply Tech Risk penalties to confidence

3. **`dashboard/static/app.js`**
   - No changes needed (pillar bar already shows tech score)

4. **`FIELD_CONTRACT.md`**
   - Document new `tech_direction` and `tech_risk` fields

---

## ml_vs_tech_conflito Threshold Analysis

### Problem

The `ml_vs_tech_conflito` scenario triggers when **Tech >= 65 AND ML <= 40**. With Tech Direction replacing Tech Score, the 65 threshold becomes much easier to reach.

### Tech Direction Frequency (17,704 H1 bars)

| Threshold | Bars | Frequency |
|-----------|------|-----------|
| >= 65 | 6,779 | 38.3% |
| >= 70 | 5,918 | 33.4% |
| >= 75 | 4,757 | **26.9%** |
| >= 80 | 3,496 | 19.7% |
| >= 85 | 2,458 | **13.9%** |
| >= 90 | 1,393 | 7.9% |

### Impact

If `ml_vs_tech_conflito` fires 15%+ of the time (with threshold 65), it's no longer a special scenario — it becomes a mode of operation. That changes its nature entirely.

The scenario was designed for **rare genuine disagreements**. The backtest must determine:
1. Does the conflict scenario still add value with Tech Direction?
2. If yes, what threshold works best?
3. If no, drop it and simplify the system.

---

## Backtest Plan

### Branch: `feature/tech-direction-split`

### Control
- Current system (tech_score with RSI/BB in score)
- `ml_vs_tech_conflito` with CONFLICT_TECH_MIN = 65

### Test Variants (all include Tech Direction + Tech Risk split)

| Variant | ml_vs_tech_conflito | Threshold | Expected Trigger Rate |
|---------|---------------------|-----------|----------------------|
| **A** | DISABLED | — | 0% |
| **B** | ENABLED | 75 | ~11% (26.9% × 40%) |
| **C** | ENABLED | 85 | ~6% (13.9% × 40%) |

### Metrics to Compare

- Win rate
- Profit factor
- Max drawdown
- Average confidence on winning vs losing trades
- Conflict scenario trigger frequency (B and C only)
- Number of trades blocked/modified by conflict scenario

### Execution

1. Create backtest branch
2. Implement Tech Direction + Tech Risk split
3. Add config flag for conflict scenario variants
4. Run 6-month backtest for Control + A + B + C
5. Generate comparison report
6. Review results together before deployment

---

## Changes in v2 (Issues Addressed)

| Issue | Problem | Resolution |
|-------|---------|------------|
| **1** | Price Action uses forming candle (intra-candle noise) | Now uses **previous closed candle only**. Reduced from 20 to 15 points. |
| **2** | EMA Alignment too binary (25/12/0) | Added **distance bonus** (0-10 pts) based on EMA separation %. |
| **3** | Tech Risk penalties too aggressive (-20 combined) | Reduced to **-8/-12** for BUYs. Diagnostic confirmed 0 winning trades would drop below 35%. |
| **4** | SELL side not analyzed | Added SELL analysis. RSI<30 is rare (1 trade). SELL penalties reduced to **-5/-8**. |
| **5** | Visual Context ±8 points unaccounted | **Removed from score**. Patterns remain in Intel Feed (informational only). |
| **6** | MACD header said 25 max but is 30 | Fixed header to **30 points max**. |

---

## Approval Checklist

- [ ] Tech Direction formula approved (Price vs EMAs, EMA Alignment with distance, MACD, Price Action on closed candle)
- [ ] Tech Risk penalties approved (-8/-12 for BUYs, -5/-8 for SELLs)
- [ ] Visual Context removal approved
- [ ] Dashboard visibility approach approved
- [ ] Backtest plan approved

**Awaiting user approval before implementation.**
