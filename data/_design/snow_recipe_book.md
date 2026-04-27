---
version: 1.0.0
source_note: |
  Recipes curated from established technical-analysis methodology — CMT
  Body of Knowledge themes (Murphy, Edwards & Magee, Schwager
  interviews), classical chart-pattern literature, candlestick patterns
  (Bulkowski, Nison), and regime-dependent confluence reading
  (Mandelbrot, Bollinger). These are NOT primary research; they are
  inspirational templates Floki can pull on demand to surface
  multi-indicator confluence patterns. Each recipe describes how
  traders historically frame a setup ("traders look for X when Y")
  rather than directing what to do. Floki retains agency.
---

# Snow Recipe Book

This file is the source of truth for `get_snow_recipe_book(category)`.
Floki calls the tool when drafting a plan and wants to see how
multi-indicator confluence is historically combined for a given
regime.

Each recipe combines two or more Snow Condition primitives. The
ingredients name the role each primitive plays in the overall
confluence — the recipe is not "use these primitives," it is "here is
how traders have framed this setup, here are the building blocks
involved." Floki picks what fits the current market read.

Categories: `trend`, `range`, `reversal`, `risk_management`.


## RECIPE: Bollinger Squeeze Breakout (Volatility Expansion)

```yaml
id: bb_squeeze_breakout
category: trend
primary_signal: bollinger_position
setup_type_alignment:
  - breakout_range
  - continuation_momentum
common_ingredients:
  - primitive: bollinger_position
    role: BB band width contracts (squeeze) — volatility compression precedes expansion.
  - primitive: atr
    role: ATR rolling average compresses, then turns up — confirms regime shift from low-vol to high-vol.
  - primitive: macd_histogram
    role: MACD histogram crossing above zero (or accelerating from below) confirms directional momentum on the breakout candle.
  - primitive: price_at_sr_zone
    role: The squeeze resolves at or through a known horizontal S/R level, giving the breakout a structural anchor.
  - primitive: indicator_crossover
    role: "One-shot — stochastic crossing above 80 (or below 20 for shorts) on the breakout bar. Stateful primitive captures the event, not the sustained state."
```

**When traders favor it:**
Traders look for this setup at the end of a low-volatility consolidation, especially when price has been carving an inside-day pattern over the previous 3–10 bars and Bollinger Band width has compressed below its 6-month average. The historical observation (Bollinger, Connors) is that volatility cycles between contraction and expansion — extended squeezes statistically resolve into directional moves more often than they resolve back into range. The setup is favored when momentum oscillators are simultaneously coiled (MACD flat near zero, ADX <20), suggesting the market hasn't yet committed.

**What it captures:**
The first 1–3 bars of the directional thrust out of compression. Edge is in early entry: by the time RSI has lifted into trending territory the move has already given back its first leg of upside. Successful trades typically run for the median ATR-based 2–4 ATR objective; reversals back through the breakout level invalidate the thesis quickly.

**Variations:**
- On lower timeframes (M15/M5): tighten BB squeeze threshold to width below 1× ATR rather than the 6-month average — the M15 noise floor is structurally tighter.
- News-anchored variant: pair with `time_between` to gate entries to the 5-minute window after a scheduled high-impact release; the squeeze-then-expansion shape often coincides with news-driven volatility unlock.
- Failure-anticipation variant: use `price_crossed_level` (one-shot latch) at the squeeze upper band, then require a sustained `price_above` 3 bars later — filters head-fake breakouts that immediately reverse.

**Framing note:**
This setup pairs naturally with `setup_type=breakout_range` or `continuation_momentum` tagging — both align with thesis-shape "ride the directional thrust." Management leans toward `trail_sl` (trail at 1× ATR) since the thesis is "the move extends"; `move_sl_to_breakeven` is a poor fit because BB-squeeze breakouts often pull back through entry on the second or third bar before continuing. Pair the recipe with `partial_close` at the first ATR objective to bank the initial thrust, leaving runner exposure for the larger expansion.


## RECIPE: Failed Breakdown Reversal (Liquidity Sweep + Reclaim)

```yaml
id: failed_breakdown_reversal
category: reversal
primary_signal: price_crossed_level
setup_type_alignment:
  - liquidity_sweep
  - structural_bounce
common_ingredients:
  - primitive: price_crossed_level
    role: "Price crosses BELOW a known support level (latch fires once) — captures the swept-low event."
  - primitive: price_above
    role: Within N bars of the sweep, price reclaims back above the broken support level — the reclaim is the trigger.
  - primitive: indicator_divergence
    role: MACD bullish divergence — sweep makes a lower low while MACD makes a higher low, suggesting fading downside momentum.
  - primitive: macd_histogram
    role: MACD histogram crossing back above zero on or shortly after the reclaim, confirming the reversal isn't just a wick.
  - primitive: price_at_sr_zone
    role: The reclaimed level is itself a meaningful S/R zone (prior swing low, daily pivot S1, weekly opening range low).
```

**When traders favor it:**
Traders look for this when an obvious support level — a recent swing low, a daily pivot, an option strike concentration — gets violated on a single impulsive bar followed immediately by a reclaim. The historical observation (Wyckoff "spring," modern liquidity-grab framing) is that the violation often represents stop-runs rather than a genuine break: limit orders below support get triggered, supply is absorbed, and price snaps back. The pattern is most reliable when the sweep happens during low-liquidity sessions (Asian, late-NY) and the reclaim happens with momentum confirmation rather than a slow drift back.

**What it captures:**
Asymmetric risk/reward at the precise moment the swept-low buyers regain control. SL placement is naturally tight — below the sweep low, which is invalidation by definition — while the upside target is the prior swing high or top of the range that defined the level. The edge is not in catching every reversal but in the well-defined invalidation: failed reclaims invalidate fast, allowing small-loss exits before the trade lingers.

**Variations:**
- News-window variant: gate the recipe to a `time_between` window AFTER a scheduled release — sweep-and-reclaim patterns are more reliable when liquidity has just been replenished.
- Multi-touch variant: require `indicator_was` showing the level has been tested at least twice in the prior 20 bars — the deeper the pool of liquidity at the sweep zone, the larger the post-reclaim move tends to be.
- Conservative variant: replace the immediate reclaim trigger with a `duration_exceeds` 3-bar pause above the level — filters out sweep-and-fade traps where price reclaims briefly then resumes lower.

**Framing note:**
This setup is the canonical case for `setup_type=liquidity_sweep` or `structural_bounce` tagging. Management favors `move_sl_to_price` (SL below the sweep low — the structural invalidation) over arbitrary BE placement. The setup's tight invalidation makes `move_sl_to_breakeven` a reasonable secondary contingency once price has cleared the prior range high. Trail_sl on the initial leg often gets whipsawed because sweep reversals frequently retest the broken level before the larger move develops.


## RECIPE: Range Fade with Mean Reversion Confluence

```yaml
id: range_fade_mean_reversion
category: range
primary_signal: bollinger_position
setup_type_alignment:
  - mean_reversion_extreme
  - structural_bounce
common_ingredients:
  - primitive: bollinger_position
    role: Price tags the upper or lower BB (above_upper / below_lower) — statistical extreme relative to the rolling 20-bar mean.
  - primitive: stochastic
    role: Stochastic in the opposite extreme (>80 for shorts at upper band, <20 for longs at lower band) — momentum at exhaustion.
  - primitive: price_at_sr_zone
    role: The BB extreme coincides with a horizontal S/R level — confluence between statistical and structural rejection.
  - primitive: indicator_divergence
    role: "Optional — RSI or MACD divergence vs the prior swing — momentum is fading even as price is making a new local extreme."
  - primitive: atr
    role: Range conditions confirmed by ATR contraction — the regime is range-bound rather than trending.
```

**When traders favor it:**
Traders look for this in markets that have been compressing into a defined range — multi-day consolidations, lunch-hour drift in equities, low-volatility sessions in FX. The historical observation (Bollinger, Connors mean-reversion research) is that extreme tags of the BB extremes during low-ATR regimes resolve back toward the band middle 65-75% of the time on the next 1-5 bars. The setup specifically is NOT favored when ATR is rising or the market is breaking out of recent range; in those regimes BB tags can sustain extension rather than reverse.

**What it captures:**
The mean-reversion edge inside an established range. Trades target the BB middle (20-period MA) or the opposite band as profit objectives. The thesis is binary at the level — either price rejects the extreme and reverts, or it doesn't and the range is breaking. Historically the higher-probability shape; lower R/R per trade but higher hit rate when filters are honored.

**Variations:**
- Time-of-day filter: pair with `time_between` for known mean-reverting windows (Asian session for FX majors, lunch hour for index futures). Adds context the price-action alone can't.
- Confluence-priority variant: require BOTH stochastic extreme AND a separate `indicator_divergence` flag — drops trade frequency by ~50% but lifts hit rate materially.
- Continuation-protection variant: use `price_crossed_level` to disarm the recipe if price closes through the BB extreme on the prior bar — the closing penetration suggests momentum continuation, not exhaustion.

**Framing note:**
Pairs naturally with `setup_type=mean_reversion_extreme` tagging. Management favors `move_sl_to_breakeven` (small profit threshold, e.g., +5 pips) because the thesis IS binary — works fast or doesn't. `close_partial` at 50% of the way to the BB middle is a common adaptation, banking the high-probability first leg before the lower-probability extension. Trail_sl misaligns with the thesis here — the trade isn't trying to capture a trend, it's harvesting noise around the mean.


## RECIPE: Trend Pullback to Moving-Average Confluence

```yaml
id: trend_pullback_ma_confluence
category: trend
primary_signal: ema_relation
setup_type_alignment:
  - pullback_trend
  - continuation_momentum
common_ingredients:
  - primitive: ema_relation
    role: Faster EMA (e.g., 20) above slower EMA (e.g., 50) on the working timeframe — confirms the trend bias.
  - primitive: price_at_fibonacci
    role: Pullback touches a 38.2% / 50% / 61.8% Fibonacci retracement of the most recent impulse leg — measured pullback depth.
  - primitive: price_at_sr_zone
    role: The Fib zone overlaps a horizontal S/R level — multi-method confluence is what distinguishes this from a single-Fib bounce.
  - primitive: stochastic
    role: Stochastic in oversold (for longs in uptrend) and curling up — the momentum reset that pullbacks produce.
  - primitive: macd_histogram
    role: MACD histogram declining but staying above zero — momentum reset without trend reversal.
```

**When traders favor it:**
Traders look for this in established trends after a measured pullback into a confluence zone — typically the third or fourth pullback within the same trend leg, where the trend is mature enough to have multiple swings to anchor Fib retracements off. The historical observation (Murphy, Edwards & Magee, Elliott wave second-wave behavior) is that trends in markets like XAU and major FX pairs spend ~40% of their travel in retracement and ~60% in impulse, and the highest-probability re-entries happen at multi-method confluence (Fib + MA + horizontal level overlapping within a few pips of each other).

**What it captures:**
Re-entry into an established trend at a measurable, non-impulsive price. Edge is in the structure — if the confluence zone holds, the trend resumes with momentum; if it breaks, the trend character has changed and exit is mechanical. The asymmetry favors the patient: most pullbacks fail at the first confluence; the ones that hold deliver the bulk of the trend distance.

**Variations:**
- Aggressive variant: enter on the first touch of the Fib zone with MACD histogram still falling — better R/R, lower hit rate.
- Conservative variant: require `indicator_crossover` (stochastic crossing back above 20 for longs) as the trigger — waits for confirmation, sacrifices some R/R for higher hit rate.
- HTF-anchor variant: require ema_relation on the HTF (e.g., H4) AS WELL as the working TF (e.g., M15) — filters out pullbacks that are actually HTF reversals masquerading as continuations on the working TF.

**Framing note:**
Pairs naturally with `setup_type=pullback_trend` or `continuation_momentum` tagging. Management is the classic case for `trail_sl` — the thesis is "the trend resumes and extends," and trailing locks in trend distance as it accumulates. A common shape is `partial_close` 33% at the first impulse high (banking the confluence-bounce edge), then trail the remainder for the larger leg. `move_sl_to_breakeven` is reasonable as a fallback contingency once the immediate retest of the confluence zone has held, but as the sole management primitive it scratches the trade on routine within-trend wiggles.


## RECIPE: Paired Hedge for Bidirectional Setups

```yaml
id: paired_hedge_bidirectional
category: risk_management
primary_signal: price_at_sr_zone
setup_type_alignment:
  - paired_hedge
common_ingredients:
  - primitive: price_at_sr_zone
    role: Price compresses at a meaningful range boundary or pre-event level — the regime is balanced and either resolution is plausible.
  - primitive: atr
    role: ATR is contracting or holding flat — not yet committed to expansion in either direction.
  - primitive: bollinger_position
    role: Price near BB middle (above_middle or below_middle, not at extremes) — neutral relative to the rolling mean.
  - primitive: time_between
    role: "The setup is gated to a specific window — typically 30-90 minutes ahead of a scheduled high-impact release or the open of a session."
  - primitive: indicator_divergence
    role: No clear directional divergence on momentum oscillators — confirms ambiguity rather than a building bias.
```

**When traders favor it:**
Traders look for this in two specific contexts: (1) the 30-90 minute window before a scheduled high-impact release (CPI, NFP, FOMC, central bank meetings) where directional commitment ahead of the release is statistically poor edge, and (2) at multi-week range boundaries where the next leg's direction depends on macro flow that hasn't yet committed. The historical observation (Schwager interviews with Jones, Druckenmiller) is that holding a position into known event risk forces a directional bet on news reaction — a different game than the technical setup that motivated the trade. Paired plans defer the directional commitment to the event itself.

**What it captures:**
The shape of the post-event move while explicitly NOT betting on its direction. One leg fires when the upside breakout condition resolves, the other expires; vice versa for the downside. The edge is in execution latency — both legs are pre-staged with full management contingencies, so when the resolution happens, entry is mechanical rather than reactive (no "I see the move, now I draft the plan, now I submit, by then it's gone"). Failure mode: both legs whipsaw back and forth, opening then closing both at scratches; this is the cost of avoiding the directional bet.

**Variations:**
- Asymmetric variant: pair a tighter SL on the lower-conviction leg with a wider SL on the higher-conviction leg — preserves the directional bias while still hedging.
- Time-decay variant: set both legs' `expires_at` to 60 minutes after the event window closes. If neither fires, both expire and you can re-evaluate fresh rather than holding stale plans into stale conditions.
- News-anchored variant: tie the entry conditions specifically to `time_between` post-release windows (e.g., +15 minutes after release time) rather than pure price triggers — reduces fakeouts during the headline-noise minute.

**Framing note:**
This is the canonical case for `setup_type=paired_hedge` tagging. Both legs typically use `move_sl_to_breakeven` for management since the thesis on each leg is binary at the event resolution — works fast or doesn't. After 30-60 minutes of post-event development, the surviving leg's character may have shifted from "binary event reaction" to "sustained directional move," at which point overlaying a `trail_sl` contingency is appropriate. The framing note in each leg should explicitly reference its paired counterpart's plan_id so reflexion / lessons can group the pair as one decision.


## RECIPE: News-Window Trade Blackout

```yaml
id: news_window_blackout
category: risk_management
primary_signal: time_between
setup_type_alignment:
  - news_reaction
common_ingredients:
  - primitive: time_between
    role: Define a window AROUND scheduled high-impact releases — e.g., -15 minutes to +15 minutes around CPI, NFP, FOMC.
  - primitive: atr
    role: Optional confirmation — ATR spiking above its rolling mean by 2x+ confirms the news-driven volatility is in effect.
  - primitive: duration_exceeds
    role: Trade has already been alive for N minutes — the news window arrives mid-trade, not pre-trade.
  - primitive: price_crossed_level
    role: Optional — if price has crossed a key structural level during the news window, the close-down should fire regardless of profit state.
  - primitive: profit_pips
    role: Optional gating — defer the blackout if the trade is already significantly in profit (e.g., >30 pips) and the news risk is "give back gains" rather than "blow up."
```

**When traders favor it:**
Traders look for this protection in two scenarios: (1) when a trade was opened on a technical setup and a high-impact release falls inside the trade's expected lifespan, and (2) when account drawdown rules require de-risking ahead of known volatility. The historical observation (Hougaard, Drobny on macro discipline) is that pre-news exits, even at small profits or scratches, statistically outperform letting positions run through releases — the asymmetry between "headline shock against you" and "headline shock for you" is structurally negative because slippage on stop-fills widens during news windows. The blackout also protects the cognitive cost of watching a position through an event (which biases subsequent decisions).

**What it captures:**
Capital preservation across known high-volatility periods. Edge is not in the individual trade's outcome but in compounding survival across many such windows — the recipe trades expected value of any single trade for reduced variance across the trading career. Common in institutional risk frameworks where "no positions during macro releases" is a hard rule rather than a discretionary call.

**Variations:**
- Hard-blackout variant: close any open position 5 minutes before the news window with no profit-gating. Blunt but reliable; suitable for accounts where consistency matters more than occasional missed wins.
- Profit-gated variant: only close positions that are at or below scratch — runners (>30 pip profit) get a `move_sl_to_breakeven` instead, banking the existing edge but keeping exposure to a favorable headline reaction.
- Scaled-de-risking variant: 30 minutes pre-news, `close_partial 50%` and trail the remainder; eliminates half the position's exposure while keeping optionality on a favorable reaction.

**Framing note:**
Pairs with `setup_type=news_reaction` tagging when the post-event re-entry is the actual trade thesis (the blackout is exit, not entry). For non-news-reaction setups that happen to span a release, this recipe is wired as an exit contingency on the existing plan — one of multiple management blocks. The blackout should typically be `priority` higher than `move_sl_to_breakeven` so it dominates in the shared tick: the news risk overrides the BE protection logic.


## RECIPE: Reversal-After-Management Protection (MFE Give-Back Guard)

```yaml
id: mfe_give_back_protection
category: risk_management
primary_signal: profit_retraced_from_peak
setup_type_alignment:
  - pullback_trend
  - continuation_momentum
  - structural_bounce
common_ingredients:
  - primitive: mfe_reached
    role: Trade has achieved a meaningful favorable excursion (e.g., MFE >= 15 pips for XAU on M15) — the management threshold is meaningful relative to the setup's natural noise.
  - primitive: profit_retraced_from_peak
    role: Price has retraced N pips from the MFE peak (e.g., 8 pips of give-back from a 15-pip MFE = 53% retrace). This is the trigger.
  - primitive: profit_pips
    role: Optional gate — only fire the protection if remaining profit is positive but below a threshold (e.g., still above scratch but below half of MFE) — the "winner becoming loser" zone.
  - primitive: indicator_crossover
    role: Optional confirmation — momentum oscillator (stochastic, MACD histogram) crossing back through neutral on the working timeframe — confirms the giveback is structural, not just noise.
  - primitive: duration_exceeds
    role: Optional — only arm the protection after the trade has been alive for N minutes. Prevents firing on intra-bar noise immediately after a fast favorable thrust.
```

**When traders favor it:**
Traders look for this in setups where the early thrust often outpaces the eventual outcome — pullback continuations, bounce-from-support trades, structural breaks where price runs hard then consolidates. The historical observation (modern execution research, Dalio "I was so right and lost so much money" essay) is that the most painful trade is the one that goes 80% of the way to TP then reverses to scratch or worse. The protection trades upside (the trade running back to original TP) for downside reduction (the give-back never reaching scratch). Win-rate-preserving rather than win-rate-improving.

**What it captures:**
The retention of partial profit on trades that achieve early thrust then stall. Edge is not in finding more winners but in keeping more of the winners you already have. Specifically targets the MFE→scratch and MFE→loser transitions, leaving MFE→TP trades unmodified. The setup is regime-aware: in trending markets where retraces routinely give back 50-70% before resuming, this recipe will fire and exit prematurely on what would have been winners. In ranging or post-thrust regimes where the initial move is often the only move, the recipe captures more of the realized edge.

**Variations:**
- ATR-anchored variant: replace fixed pip thresholds with ATR-based ones — `profit_retraced_from_peak.pips = 0.5 * ATR` — adapts to current volatility regime.
- Two-stage variant: first stage `close_partial 50%` at MFE retrace 30%; second stage `close_full` at MFE retrace 60%. Banks the easy first leg, accepts more give-back on the runner.
- Trend-protective variant: only arm the protection when ema_relation has flipped against the trade direction on the working TF — confirms regime change rather than mere noise.

**Framing note:**
Pairs with multiple setup_types — particularly the trend-continuation family where give-back is the most painful outcome. Wires as a contingency block alongside the entry-thesis-driven exit (e.g., "close at TP" and "close on opposite RSI cross") rather than replacing them. The two-stage variant is often paired with `trail_sl` on the runner: the partial close locks in the easy edge, the trail captures further extension, and the give-back protection catches the case where neither hard exit fires. Explicitly designed for setups where `move_sl_to_breakeven` alone leaves too much edge on the table — BE-only fires only at scratch, while this recipe fires at "winner becoming loser" zones.


## RECIPE: Multi-Stage Scaling Out (MFE Milestones + Partial Closes)

```yaml
id: multi_stage_scaling_out
category: risk_management
primary_signal: mfe_reached
setup_type_alignment:
  - continuation_momentum
  - pullback_trend
  - breakout_range
common_ingredients:
  - primitive: mfe_reached
    role: Multiple thresholds — e.g., 10 pips, 20 pips, 35 pips — each anchoring a separate management contingency.
  - primitive: profit_pips
    role: Used as gate for the trail_sl contingencies that follow the partial closes — only trail the runner once initial scale-out has occurred.
  - primitive: price_at_fibonacci
    role: Optional milestone alignment — scaling out at 1.272 / 1.618 Fibonacci extensions of the entry impulse rather than fixed pip thresholds — extension targets are regime-natural.
  - primitive: price_at_sr_zone
    role: Alternative milestone alignment — scale out at known horizontal resistance levels (or support for shorts) — structural targets where rejections are historically more likely.
  - primitive: atr
    role: ATR-based scaling threshold variant — scale out at 1x, 2x, 3x ATR multiples from entry — normalizes to current volatility.
```

**When traders favor it:**
Traders look for this in setups where the expected favorable distance is large and uncertain — trend-continuation impulses, breakout legs, post-news directional plays. The historical observation (Tharp position-sizing research, Van Tharp "expectancy" framing) is that single-target plans force a binary outcome (full TP hit or trade scratches), while multi-stage scaling produces a probability-weighted distribution: small banks on routine moves, fuller exposure on the runners that genuinely extend. Particularly favored when the trade is sized larger than the trader's typical comfort, since partial closes reduce position size at predictable milestones rather than via reactive cutting.

**What it captures:**
The asymmetric distribution of trade outcomes — most setups produce 1-2x risk in profit, a few produce 3-5x or more. Single-target plans either close at the first target (capping the runners) or hold for the runner (eating the full give-back when it doesn't materialize). Multi-stage scaling captures both regimes within the same plan: lock the routine edge with early partials, leave runner exposure with the trail. Edge is in the distribution shape, not the per-trade outcome.

**Variations:**
- Three-stage classic: `close_partial 33%` at MFE 1x ATR, another `33%` at 2x ATR, then trail the final third. Each stage banks an even slice of the position; the runner has unlimited theoretical upside.
- Asymmetric early-bank: `close_partial 50%` at the first milestone (banking the easy edge fast), `close_partial 25%` at the second, trail the remaining 25%. Suited for noisy markets where the first milestone is hit reliably but extension is uncertain.
- Structural-target variant: replace pip/ATR milestones with `price_at_sr_zone` or `price_at_fibonacci` triggers — natural-target scaling that adapts to the chart's existing structure rather than a fixed pip ladder.

**Framing note:**
Pairs naturally with continuation-family `setup_type` tagging (`continuation_momentum`, `pullback_trend`, `breakout_range`). Each milestone is a separate management contingency block — a single plan can carry 3-5 such contingencies, each with its own `priority` and `fires=once`. Combine with `trail_sl` on the runner stage and optionally `mfe_give_back_protection` (above) as a fallback for the final stage. This recipe is the explicit alternative to the single-`move_sl_to_breakeven` pattern — it harvests the trade across its lifecycle rather than at a single point in time.


## RECIPE: Trend Exhaustion at HTF Resistance with Divergence

```yaml
id: trend_exhaustion_divergence
category: reversal
primary_signal: indicator_divergence
setup_type_alignment:
  - divergence_play
  - mean_reversion_extreme
common_ingredients:
  - primitive: indicator_divergence
    role: MACD or RSI bearish divergence on the working timeframe — price makes a higher high, oscillator makes a lower high, signaling weakening momentum.
  - primitive: price_at_sr_zone
    role: The new high prints AT or just into a higher-timeframe resistance zone — H4 / D1 supply, prior swing high, weekly range upper bound.
  - primitive: bollinger_position
    role: Price tags or pierces the upper Bollinger Band on the working TF — extreme distance from the rolling mean.
  - primitive: stochastic
    role: Stochastic in overbought (>80) and curling down — momentum oscillator confirming exhaustion alongside the divergence.
  - primitive: indicator_was
    role: Stateful — confirms the divergence was present in the prior 3-5 H1 bars (not just the current tick), filtering one-bar noise.
```

**When traders favor it:**
Traders look for this in the late stage of an extended trend where price has been making sequential higher highs but momentum oscillators have been stalling or rolling over. The historical observation (Pring on momentum, Wilder's original RSI work, Elder triple-screen methodology) is that bearish divergence at a confirmed HTF resistance has substantially better statistical edge than divergence in mid-range or against weak resistance — the structural level provides the rejection mechanism that the divergence alone cannot. The pattern is most reliable on 4th-and-later trend swings where the bull case is well-distributed and contrarian positioning is starting to build.

**What it captures:**
The transition from trend-extension regime to trend-reversal or consolidation regime. Trades target the prior swing low or BB middle as profit objectives — modest distance, asymmetric R/R because the SL is naturally tight (above the rejection wick at the HTF level). Failure mode: false rejection followed by another leg up; classic "the trend is your friend until it ends, but trying to call the end is expensive."

**Variations:**
- HTF-confluence variant: require the resistance level to be referenced on TWO higher timeframes (e.g., H4 swing high AND D1 fib level) — drops false rejection rate at the cost of fewer signals.
- Volume-anchored variant: pair with confirmation that the new high printed on lower volume than the prior swing's high (volume divergence) — adds a third independent confirmation axis.
- Conservative-trigger variant: require `price_crossed_level` confirming price has subsequently crossed BACK BELOW the prior swing high after the divergent print — waits for confirmation rather than predicting the rejection.

**Framing note:**
Pairs with `setup_type=divergence_play` or `mean_reversion_extreme` tagging. Management favors a tight `move_sl_to_breakeven` (the thesis is "rejection works fast or it doesn't") combined with `close_partial 50%` at the first BB middle target. Trail_sl misaligns with the rejection-harvesting thesis (the setup isn't trend-capturing). If price extends past the rejection level by 1+ ATR, the divergence has failed and the trade typically exits on emergency rather than chasing further drawdown.


## RECIPE: Range Accumulation with Volume-Profile Anchor

```yaml
id: range_accumulation_anchor
category: range
primary_signal: price_at_sr_zone
setup_type_alignment:
  - structural_bounce
  - mean_reversion_extreme
common_ingredients:
  - primitive: price_at_sr_zone
    role: Price approaches a level identified by repeated prior reactions — a high-volume node (HVN) on multi-day volume profile, a long-tested S/R zone, or a recent accumulation shelf.
  - primitive: indicator_was
    role: Stateful — the level has been tested at least 2-3 times in the prior 50 bars without breaking, confirming "accumulation" rather than a single-touch level.
  - primitive: atr
    role: ATR is contracting on approach — the absorption is happening with declining volatility, suggesting passive supply/demand absorption rather than rejection.
  - primitive: stochastic
    role: Stochastic at the matching extreme (oversold for support tests, overbought for resistance tests), consistent with a counter-trend bounce setup.
  - primitive: bollinger_position
    role: Price near the lower BB (for support tests) or upper (resistance tests) — BB confirms the touch is at the statistical edge of recent range.
```

**When traders favor it:**
Traders look for this at multi-day or multi-week levels where price has demonstrated absorption (multiple tests without a clean break) and current approach shows declining ATR. The historical observation (Wyckoff accumulation/distribution framework, market profile theory) is that volume nodes accumulated over multiple sessions act as magnetic levels — price returns to them frequently and bounces are statistically reliable while the level holds. The setup specifically distinguishes between "level approached for the first time on increasing volatility" (likely break, fade the bounce) and "level revisited multiple times on declining volatility" (likely hold, take the bounce).

**What it captures:**
The hold of an accumulated level with high statistical reliability per touch. Edge is in the well-defined invalidation: a clean break of the level (1+ ATR through, with momentum) ends the recipe immediately. Trades target the opposite side of the recent range; profit-distance is bounded by the range itself rather than open-ended.

**Variations:**
- Volume-confirmation variant: pair with confirmation that current touch is occurring on lighter tick volume than the prior break attempt — the absorption is ongoing rather than challenged.
- Time-sensitive variant: require the level to have been tested most recently within the past N bars (e.g., 20-50 bars on M15) — older levels are less reliable as accumulation magnets fade with time.
- Re-test confirmation variant: instead of entering on the touch itself, wait for the touch + a small bounce + a re-test, then enter on the re-test hold — sacrifices entry price for confirmation.

**Framing note:**
Pairs naturally with `setup_type=structural_bounce` or `mean_reversion_extreme` tagging. Management leans toward `close_partial 50%` at the range midpoint, then trail or BE-lock the runner toward the opposite range boundary. The setup's well-defined invalidation (clean break of the level) makes a tight `move_sl_to_price` (a few pips beyond the level) the natural primary protection — `move_sl_to_breakeven` is reasonable as a secondary contingency once price has cleared the immediate retest zone.


## RECIPE: Double Top / Double Bottom with Momentum Confirmation

```yaml
id: double_top_bottom_pattern
category: reversal
primary_signal: price_at_sr_zone
setup_type_alignment:
  - structural_bounce
  - divergence_play
common_ingredients:
  - primitive: price_at_sr_zone
    role: Two distinct touches of approximately the same price level — the second touch is the trade trigger.
  - primitive: indicator_was
    role: Stateful — confirms the prior touch (first peak/trough) occurred within the prior N bars at the matching extreme, anchoring the pattern temporally.
  - primitive: indicator_divergence
    role: MACD or RSI divergence between the two tops/bottoms — first peak prints stronger momentum than second, confirming weakening drive.
  - primitive: macd_histogram
    role: MACD histogram crossing back through zero after the second touch — confirms momentum has actually rolled rather than just stalled.
  - primitive: price_crossed_level
    role: Pattern completes when price crosses the "neckline" — the swing low between the two tops (for double-top) or swing high between the two bottoms (for double-bottom).
```

**When traders favor it:**
Traders look for this as a classical reversal pattern documented across virtually all chart-pattern literature. The historical observation (Edwards & Magee, Bulkowski statistical pattern surveys) is that double-top / double-bottom patterns at HTF reversal levels have statistically better continuation rates than those formed in mid-range, and the addition of momentum divergence between the two touches lifts the reliability further. The pattern is most reliable when the two touches are separated by 5-30 bars — too close suggests still consolidating, too far suggests a fully separate setup.

**What it captures:**
A structural shift from continuation to reversal at a documented level. The neckline cross (or breach in classical literature) is the trigger that confirms the pattern; entry before the cross is anticipatory and lower hit-rate. Trades target the measured-move objective (height of the pattern projected from the neckline) — Bulkowski's research suggests the median outcome reaches ~60-70% of the measured move before stalling.

**Variations:**
- Anticipatory variant: enter on the second touch hold rather than waiting for the neckline cross — better R/R, lower hit rate; suitable when divergence is strong and HTF context favors the reversal.
- Triple-touch variant: extend the recipe to triple-top / triple-bottom (3 touches) — fewer signals but stronger statistical edge per Bulkowski.
- Volume-classical variant: require the second touch to print on lower tick-volume than the first — adds the classical volume confirmation Edwards & Magee specify.

**Framing note:**
Pairs with `setup_type=structural_bounce` or `divergence_play` tagging depending on whether the divergence component or the pattern component is the load-bearing thesis. Management favors `close_partial 50%` at the first major resistance/support past the neckline (often a prior swing point), with `trail_sl` on the runner toward the measured-move target. The pattern's measured-move framework gives a natural TP that often outpaces simple R-multiple targets — sized correctly, scaling out at the measured target captures the bulk of the documented edge.


## RECIPE: Engulfing Candle at Confluence (Candlestick Reversal)

```yaml
id: engulfing_candle_confluence
category: reversal
primary_signal: price_at_sr_zone
setup_type_alignment:
  - structural_bounce
  - mean_reversion_extreme
common_ingredients:
  - primitive: price_at_sr_zone
    role: Price tests a known horizontal S/R level — daily pivot, prior swing, weekly opening range, fib retracement.
  - primitive: bollinger_position
    role: The candle prints at or beyond the BB extreme on the working timeframe — statistical extreme alongside the structural level.
  - primitive: stochastic
    role: Stochastic at the matching momentum extreme (oversold for bullish engulfing, overbought for bearish) — three-axis confluence with structure and statistics.
  - primitive: indicator_was
    role: Stateful — the prior candle was in the opposite direction (e.g., a red candle preceding the bullish engulfing) and the engulfing fully reverses it.
  - primitive: macd_histogram
    role: Optional — MACD histogram beginning to roll in the engulfing direction confirms the candle isn't a one-bar wick.
```

**When traders favor it:**
Traders look for this where Japanese candlestick literature has documented reliable single-bar reversals — particularly bullish/bearish engulfings at HTF confluence levels. The historical observation (Nison's "Japanese Candlestick Charting Techniques," Bulkowski candlestick statistical surveys) is that engulfing patterns are *most* reliable at structural levels (S/R, fib, pivot) and *least* reliable in mid-range or trending conditions. The candle's body fully covering the prior bar's range encodes a meaningful intra-bar shift — sellers (or buyers) failing to maintain control across the bar.

**What it captures:**
A high-conviction single-bar reversal trigger at a structural level, with naturally tight invalidation (the high or low of the engulfing candle itself is the SL). Edge is in execution clarity — the entry is mechanical (close above engulfing high for longs, below for shorts, or break-of-engulfing-range trigger). Failure mode: the engulfing forms but price never breaks the trigger level, expiring the setup.

**Variations:**
- Inside-bar-confirmation variant: require the bar AFTER the engulfing to be an inside bar (lower high, higher low) — confirms the rejection has held through one additional bar of testing.
- Volume-anchored variant: require the engulfing candle to print on higher tick-volume than the prior 5 bars — Nison's "with conviction" criterion.
- HTF-context variant: only fire if the working-TF engulfing aligns with HTF momentum (e.g., bullish engulfing on M15 within an H4 uptrend pullback) — drops counter-trend signals at the cost of fewer reversals.

**Framing note:**
Pairs with `setup_type=structural_bounce` tagging. Management favors `move_sl_to_breakeven` with a small profit threshold (e.g., +5 pips) since the thesis is fast-binary, combined with `close_partial 50%` at the prior swing point as the first profit milestone. Trail_sl misaligns with the point-trade nature of candlestick reversals (they aren't trend-capture trades). If price extends past the swing point cleanly, the trade has overdelivered relative to the recipe's thesis — further extension is bonus rather than expected.


## RECIPE: Multi-Timeframe Trend Alignment Entry

```yaml
id: mtf_trend_alignment
category: trend
primary_signal: ema_relation
setup_type_alignment:
  - continuation_momentum
  - pullback_trend
common_ingredients:
  - primitive: ema_relation
    role: HTF (e.g., H4) EMA stack confirms trend bias — fast EMA above slow EMA for longs, opposite for shorts.
  - primitive: ema_relation
    role: Working TF (e.g., M15) EMA stack independently confirms the same bias — alignment across two TFs.
  - primitive: macd_histogram
    role: MACD histogram on the working TF positive (for longs) or negative (for shorts) — momentum aligned with trend.
  - primitive: price_at_sr_zone
    role: Entry trigger at a working-TF support (longs) or resistance (shorts) — price pulls back into the structure within the aligned trend.
  - primitive: stochastic
    role: Stochastic on the working TF in the opposite extreme (oversold for longs in uptrend, overbought for shorts in downtrend) — pullback confirmation within the trend.
```

**When traders favor it:**
Traders look for this in established trends where multi-timeframe alignment compounds the edge. The historical observation (Elder triple-screen, Murphy MTF analysis, Schwager interviews on systematic trend-following) is that trades aligned across at least 2 timeframes have substantially higher hit rates than single-TF trades in similar setups — the HTF context filters counter-trend setups that look attractive on the working TF in isolation. The pattern is favored when both TFs are clearly trending (not consolidating or transitioning) and the working-TF pullback is into a structural level rather than mid-air.

**What it captures:**
The intersection of structural pullback and trend-alignment edge. Trades target the recent working-TF swing high (longs) or swing low (shorts) as initial profit, with optional extension toward HTF projected move. Edge compounds: the HTF trend gives the setup directional bias, the working-TF pullback gives a measurable entry, and the alignment filter screens out marginal setups that fail outside trend regimes.

**Variations:**
- Three-TF variant: require alignment across HTF (D1), context TF (H4), AND working TF (M15) — drops signal frequency materially but lifts hit rate to systematic-strategy levels.
- Pullback-depth variant: require the working-TF pullback to reach a minimum percentage of the prior impulse (e.g., 38.2% Fib) — filters out shallow pullbacks that often fail.
- Momentum-trigger variant: instead of static entry on level touch, require `indicator_crossover` on stochastic crossing back above 20 (longs) — waits for momentum confirmation.

**Framing note:**
Pairs with `setup_type=pullback_trend` or `continuation_momentum` tagging. Management is the canonical case for `trail_sl` (trail at HTF ATR distance) since the thesis is "the aligned trend extends." A common shape combines `close_partial 33%` at the first impulse high (banking the aligned-pullback edge), trail the remainder, and add `mfe_give_back_protection` (separate recipe) as runner safety. Sole `move_sl_to_breakeven` underdelivers here: in MTF-aligned trends, post-entry pullbacks below entry are routine, and BE-only scratches what is statistically the highest-edge setup family available.


## RECIPE: Session Open Range Break

```yaml
id: session_open_range_break
category: trend
primary_signal: price_crossed_level
setup_type_alignment:
  - session_open_break
  - breakout_range
common_ingredients:
  - primitive: time_between
    role: "Setup is gated to the first 30-90 minutes of a major session — Tokyo open (00:00 UTC), London open (07:00-08:00 UTC), or NY open (13:30-14:30 UTC)."
  - primitive: price_crossed_level
    role: Stateful — the high or low of the prior-session range serves as the breakout level. The latch fires when the open session crosses through that level.
  - primitive: atr
    role: ATR on the working TF expanding above its prior-session-end value — the new session is bringing volatility back, not extending the prior session's drift.
  - primitive: indicator_was
    role: "Stateful filter — the prior session was rangebound (e.g., the prior 6-12 H1 bars showed contracting range), confirming this is a true open break, not a continuation of an already-trending move."
  - primitive: macd_histogram
    role: MACD histogram on the working TF aligning with the breakout direction within 1-2 bars of the level cross — momentum confirmation alongside the structural break.
```

**When traders favor it:**
Traders look for this at major session opens after a quiet prior session — particularly Tokyo Asian range break into London open, or London range break into NY open. The historical observation (institutional flow research, ICT methodology, Steidlmayer market profile) is that session opens disproportionately concentrate institutional order flow, and breaks of the prior session's range during the first session hour are statistically more likely to extend than mid-session breaks. The pattern is favored when the prior session was rangebound (confirmed by ATR contraction or compressed bar ranges); high-volatility prior sessions blur the signal.

**What it captures:**
The directional commitment of the new session as institutional desks express positioning. Edge is concentrated in the first 1-2 hours of the session — after that, the open-range-break framework loses its statistical anchor and the move becomes a normal trend-continuation setup. Trades target the prior session's range height projected from the breakout level (measured-move objective), commonly 0.5x to 1x the prior range.

**Variations:**
- Tokyo-range variant: specifically use the Tokyo session range (00:00-07:00 UTC) as the level set, breaking into London open. Reliable on majors and gold.
- IB-break variant: use the first 60 minutes of the new session as an "Initial Balance" range, then trade breaks of THAT IB on the next 60-120 minutes. Steidlmayer's classical framework.
- Volume-anchored variant: require the breakout candle to print on tick volume above the prior 20-bar median — confirms institutional participation rather than light-volume probing.

**Framing note:**
This is the canonical case for `setup_type=session_open_break` tagging. Management favors `trail_sl` (trail at 0.5x ATR) since the thesis is "the session-driven move extends," combined with `close_partial 50%` at the measured-move target. Sole `move_sl_to_breakeven` underdelivers here — session opens often retest the broken level within the first hour before continuing, and BE-only scratches what is structurally a high-edge setup family. The setup naturally pairs with a `time_between` exit contingency: if the trade is still open 2-3 hours into the session without progress, the open-break framework has lapsed and exiting at scratch is preferable to holding into mid-session noise.
