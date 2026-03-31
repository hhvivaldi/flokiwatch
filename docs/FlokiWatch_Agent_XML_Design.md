# FlokiWatch Agent XML Data Package — Design Specification

## Purpose
Replace the current raw JSON dump with XML-tagged sections that align with how the Agent is instructed to think: Structure → Macro → Indicators → Context. Each section is clearly delineated so Claude processes data in the correct analytical order.

## Current Format (BEFORE)
```
## CURRENT MARKET DATA
This is your independent H1 market snapshot...

```json
{entire data_package as JSON blob — 300+ lines, everything mixed together}
```
```

## New Format (AFTER)
```
## INDEPENDENT H1 MARKET SNAPSHOT
Analyze the raw market data below. Read structure first, then macro, then indicators. What would YOU trade right now?

<snapshot_time>2026-03-10T19:00:13</snapshot_time>

<current_price bid="5224.84" ask="5225.16" spread="3.2"/>

--- SECTION 1: PRICE STRUCTURE (Read this FIRST) ---

<price_structure>
  <h1_candles count="20" description="Last 20 hourly candles, newest last">
    {time}, {o}, {h}, {l}, {c}, {v}
    {time}, {o}, {h}, {l}, {c}, {v}
    ... (20 rows)
  </h1_candles>

  <h4_candles count="20" description="Last 20 four-hour candles, newest last">
    ... (20 rows)
  </h4_candles>

  <d1_candles count="10" description="Last 10 daily candles, newest last">
    ... (10 rows)
  </d1_candles>

  <m5_candles count="10" description="Last 10 five-minute candles for micro-structure">
    ... (10 rows)
  </m5_candles>

  <mtf_trend d1="bullish" h4="bearish"/>

  <candlestick_pattern>
    <primary name="Bearish Engulfing" direction="bearish" score="-4.0" sr_multiplier="1.00"/>
    <all_patterns>Bearish Engulfing, Doji</all_patterns>
    <sr_context>No strong S/R zone nearby</sr_context>
  </candlestick_pattern>

  <support_resistance>
    <nearest_support level="5208.35" distance_pips="1649"/>
    <nearest_resistance level="5236.42" distance_pips="1158"/>
    <proximity near_strong_zone="false" nearest_dist_pips="1158" nearest_info="resistance at 5236.42"/>
    <zones count="8">
      <zone price="5208.35" type="support" touches="12" timeframe="H4" strength="strong" dist_pips="1649" position="below" confluence="false"/>
      <zone price="5236.42" type="resistance" touches="5" timeframe="H4" strength="strong" dist_pips="1158" position="above" confluence="false"/>
      ... (8 zones total)
    </zones>
  </support_resistance>
</price_structure>

--- SECTION 2: MACRO CONTEXT (Read this SECOND) ---

<macro_context>
  <dxy value="98.67" change_pct="-0.52" trend="falling" impact="bullish for gold"/>
  <vix value="22.98" change_pct="-9.88"/>
  <yields_10y value="4.12" change_pct="-0.27"/>
  <calendar phase="normal" bias="NEUTRAL" source="mt5_bridge"/>
  <sentiment overall="mixed"/>
  <headlines count="20">
    <headline time="17:52 UTC" text="US Dollar Index retreats from Iran war highs..." score="30.0"/>
    <headline time="17:10 UTC" text="Middle East tensions raise the stakes..." score="70.0"/>
    <headline time="16:22 UTC" text="Oil prices seesaw as Trump sends mixed messages..." score="50.0"/>
    ... (up to 20 headlines)
  </headlines>
</macro_context>

--- SECTION 3: TECHNICAL INDICATORS (Read this THIRD — adjust confidence, don't decide direction) ---

<indicators>
  <rsi value="87.8" condition="overbought"/>
  <macd value="X" signal="Y" histogram="Z" trend="bullish/bearish"/>
  <emas ema9="5220" ema21="5195" ema50="5138" price_vs_ema50_pct="+1.7%"/>
  <bollinger upper="5260" middle="5180" lower="5100" position="0.85"/>
  <atr value="45.2" description="Average True Range H1"/>
  <adx value="17.7" plus_di="30.5" minus_di="15.9" trend_strength="weak"/>
  <volume ratio="0.43" classification="very_low"/>
</indicators>

<ml_predictions>
  <prediction direction="bearish" confidence="72%"/>
  <h1_probability>0.41</h1_probability>
  <h4_probability>0.27</h4_probability>
  <ensemble_agreement>high</ensemble_agreement>
  <pattern>bearish continuation</pattern>
</ml_predictions>

--- SECTION 4: TRADING CONTEXT ---

<volatility status="NORMAL" m5_move_pct="-0.11" cooling_until="null"/>

<session name="NY" hour_utc="19">
  <today trades="0" wins="0" losses="0" pnl="0.00"/>
  <last_5_results>WIN, LOSS, WIN, WIN, LOSS</last_5_results>
  <consecutive_losses>0</consecutive_losses>
</session>

<open_positions count="0"/>

<trade_feedback>
  <last_trades>
    ... (recent trade outcomes if any)
  </last_trades>
  <agent_accuracy>
    ... (agent's own track record if available)
  </agent_accuracy>
</trade_feedback>

---

Based on this data: What does the price structure tell you? What is your decision? Respond with valid JSON (OPEN_BUY, OPEN_SELL, or WAIT only).
```

## Design Principles

### 1. Order matches the Agent's thinking process
- Section 1 (Price Structure) = Structure First
- Section 2 (Macro Context) = Macro Second
- Section 3 (Indicators) = Indicators Third, as adjustment
- Section 4 (Trading Context) = Situational awareness

### 2. Each section has a clear purpose comment
- "Read this FIRST", "Read this SECOND", "Read this THIRD — adjust confidence, don't decide direction"
- These reinforce the prompt's Structure → Macro → Indicators hierarchy directly in the data

### 3. Attributes for quick scanning, content for detail
- `<rsi value="87.8" condition="overbought"/>` — Claude sees the key number instantly
- `<nearest_support level="5208.35" distance_pips="1649"/>` — no need to parse nested JSON

### 4. Candles as compact rows, not verbose JSON
- Instead of 20 JSON objects with repeated key names, use compact rows:
  `2026-03-10T18:00, 5228.50, 5235.80, 5215.20, 5224.84, 1250`
- Saves tokens (less repetition of "time", "o", "h", "l", "c", "v" for every row)
- Header in the tag describes the columns

### 5. Nothing is lost
Every field from the current skeleton is mapped:
- timestamp → snapshot_time
- current_price → current_price attributes
- h1/h4/d1/m5_candles → price_structure section
- indicators → indicators section
- ml_predictions → ml_predictions section
- macro → macro_context section
- mtf_trend → price_structure (structural context)
- sr_zones + sr_proximity + nearest_support/resistance → support_resistance
- candlestick_patterns → candlestick_pattern
- positions → open_positions
- session → session section
- trade_feedback → trade_feedback section
- volatility → volatility section

### 6. What was REMOVED (by design, for Proactive independence)
- brain_analysis — no Brain opinion in Proactive
- agent_memory_context — no previous reject memory in Proactive

## Implementation Notes

### Files to change:
- `agent_data_builder.py` — new function `format_proactive_xml(data_package)` that converts the dict to XML string
- `ai_agent.py` — `_build_user_message()` for PROACTIVE_H1 uses XML format instead of JSON dump
- No changes to `build_proactive_data_package()` — the data stays the same, only the FORMAT changes

### Candle format optimization:
Current JSON format per candle: ~120 chars
```json
{"time": "2026-03-10T18:00:00", "o": 5228.50, "h": 5235.80, "l": 5215.20, "c": 5224.84, "v": 1250}
```

Proposed compact format per candle: ~65 chars
```
2026-03-10T18:00, 5228.50, 5235.80, 5215.20, 5224.84, 1250
```

With 60 candles (20 H1 + 20 H4 + 10 D1 + 10 M5), this saves ~3300 chars / ~800 tokens per call.

### Token impact estimate:
- Current JSON dump: ~4000-6000 tokens
- Proposed XML format: ~3000-4500 tokens (savings from compact candles + less key repetition)
- Net saving: ~1000-1500 tokens per call (~$0.001-0.002 saved per call)

## Validation Plan
After implementation:
1. Dev shows the FULL XML message for one proactive call
2. We verify every data point from the skeleton is present in the XML
3. We verify the Agent's response quality doesn't degrade
4. If Agent produces better/same quality responses with XML, we extend XML format to the SIGNAL path too

---

*FlokiWatch Agent XML Data Package Design | March 10, 2026*
