"""Apply the 8 paired-plans-deprescription edits to agent_prompts.py atomically.
Asserts each old block is found verbatim before replacing — bails on any miss
so the file is never partially-edited."""
import io, sys

PATH = 'agent_prompts.py'
BS = chr(92)
DASH = BS + 'u2014'  # literal six chars as the file stores em-dashes
ARROW = BS + 'u2192'  # literal six chars for ->

with io.open(PATH, 'r', encoding='utf-8') as f:
    text = f.read()

edits = []

# ---- Change 1: Line 17 — TradingView shape (c) ----
old1 = ("Three TradingView shapes that drive most cycles: (a) decision-point setup "
        + ARROW + " 2-3 scenarios mapped from the current level with directional triggers; "
        "(b) descending or ascending channel " + ARROW + " bounce/rejection levels at each "
        "boundary with reclaim triggers; (c) converging triangle / range pre-breakout "
        + ARROW + " support and resistance with both breakout directions encoded as paired plans.")
new1 = ("Three TradingView shapes that drive most cycles: (a) decision-point setup "
        + ARROW + " 2-3 scenarios mapped from the current level with directional triggers; "
        "(b) descending or ascending channel " + ARROW + " bounce/rejection levels at each "
        "boundary with reclaim triggers; (c) converging triangle / range pre-breakout "
        + ARROW + " identify the support, resistance, and the breakout direction your read "
        "favours; encode that one. If you genuinely have no directional read, you can wait "
        "for the resolution and act on the next cycle " + DASH + " you are not required to "
        "encode both legs.")
edits.append(("Line 17 — TradingView shape (c)", old1, new1))

# ---- Change 3: PAIRED PLANS paragraph (line 126) ----
old3 = ("PAIRED PLANS " + DASH + " for genuinely bidirectional setups (range pre-event, "
        "undecided breakout, post-news whip protection), submit TWO plans in the same "
        "cycle: one for the BUY scenario, one for the SELL scenario. Each is a complete "
        "plan with its own entry/management/exit/emergency. They do not interfere "
        + DASH + " Snow watches both independently; whichever side the market chooses "
        "fires its plan, the other expires. Do not hesitate to submit two plans on a "
        "single cycle when the market hasn't picked a side. Two `submit_plan_to_snow` "
        "calls in the same cycle is the canonical shape for \"ambiguous setup with both "
        "legs encoded.\"")
new3 = ("AMBIGUOUS SETUPS " + DASH + " when the market hasn't picked a side, analyze and "
        "take a position. You are not required to cover both directions. If your read is "
        "\"post-news whip, both sides plausible,\" that is a thesis: encode the side your "
        "analysis actually favours, or wait if no side reads cleanly enough to act on. Do "
        "not author a counter-direction plan as a hedge unless you have an independent "
        "thesis for that direction at a distinct level with its own invalidation. "
        "Same-direction multi-plans (e.g. a breakout BUY at one level and a deeper bounce "
        "BUY at a different level) remain encouraged when each has its own setup_type and "
        "trigger " + DASH + " see ANTI-CONFLATION below.")
edits.append(("Line 126 — PAIRED PLANS replaced with AMBIGUOUS SETUPS", old3, new3))

# ---- Change 4: MULTI-PLAN BATCHING DISCIPLINE first sentence (line 128) ----
old4 = ("MULTI-PLAN BATCHING DISCIPLINE " + DASH + " when submitting multiple plans in "
        "the same cycle (PAIRED PLANS or CONCURRENT PLANS), emit each "
        "`submit_plan_to_snow` call in its OWN assistant turn " + DASH + " wait for the "
        "tool result of plan #1 before emitting plan #2's tool_call.")
new4 = ("MULTI-PLAN BATCHING DISCIPLINE " + DASH + " when submitting multiple plans in "
        "the same cycle, emit each `submit_plan_to_snow` call in its OWN assistant turn "
        + DASH + " wait for the tool result of plan #1 before emitting plan #2's tool_call.")
edits.append(("Line 128 — MULTI-PLAN BATCHING DISCIPLINE intro", old4, new4))

# ---- Change 5: PLANS ARE SCENARIOS NOT PREDICTIONS (line 187) ----
old5 = ("PLANS ARE SCENARIOS, NOT PREDICTIONS " + DASH + " A plan is a scenario, not a "
        "prediction. You don't need to believe it will happen " + DASH + " you need to "
        "recognize it COULD happen. Map every reasonable path the chart shows: if price "
        "breaks support, what's the trade? If it reclaims resistance, what's the trade? "
        "If it ranges, where are the boundaries? Each path gets a plan. The confidence "
        "field reflects how clean the setup is, not how likely the scenario is " + DASH +
        " a well-structured plan at an unlikely level can still be confidence=70. The "
        "TradingView shapes that drive most cycles " + DASH + " \"3 scenarios for today\" "
        "with arrows up/sideways/down, converging triangles with both breakout legs "
        "marked, channels with bounce AND breakdown paths " + DASH + " are scenario "
        "maps, not single-direction predictions. Each leg gets its own plan; the chart "
        "maps possibilities, you encode them, Snow watches.")
new5 = ("PLANS ARE SCENARIOS, NOT PREDICTIONS " + DASH + " A plan is a scenario, not a "
        "prediction. You don't need to be certain it will happen " + DASH + " you need "
        "to recognize it has a clean enough setup that you'd take the trade if the "
        "conditions go all-true. Encode the scenarios your read actually favours; you "
        "are NOT required to encode every possible path. The confidence field reflects "
        "how clean the setup is, not how likely the scenario is " + DASH + " a "
        "well-structured plan at an unlikely level can still be confidence=70. The "
        "TradingView shapes that drive most cycles " + DASH + " decision-point setups, "
        "converging triangles, channels " + DASH + " describe the levels that matter; "
        "encode the side(s) of those levels your analysis actually favours, not all "
        "sides as a default.")
edits.append(("Line 187 — PLANS ARE SCENARIOS, NOT PREDICTIONS", old5, new5))

# ---- Change 6: WORKED FLOW step 3 (line 383) ----
old6 = ("3. Form a thesis " + DASH + " directional bias, ambiguous-with-branches, or "
        "genuinely bidirectional (when the market is balanced ahead of an event or at a "
        "key inflection, a paired BUY-leg + SELL-leg plan is the right shape " + DASH +
        " see PAIRED PLANS above).")
new6 = ("3. Form a thesis " + DASH + " directional bias, ambiguous-with-branches (one "
        "plan with conditional triggers), or no-trade. If the market reads as genuinely "
        "50/50, WAIT is a valid outcome; you do not need to encode both directions to "
        "\"cover\" the ambiguity.")
edits.append(("Line 383 — WORKED FLOW step 3", old6, new6))

# ---- Change 7a: WORKED FLOW step 4 last sentence (line 384) ----
old7a = "emergency (max_loss_pips + max_duration_minutes). For paired plans, draft two complete plans, one per direction."
new7a = "emergency (max_loss_pips + max_duration_minutes). For multiple plans this cycle, draft each as a complete standalone plan."
edits.append(("Line 384 — WORKED FLOW step 4 paired-plans tail", old7a, new7a))

# ---- Change 7b: WORKED FLOW step 5 (line 391) ----
old7b = "5. submit_plan_to_snow(plan) " + DASH + " one call per plan; for paired plans, two consecutive calls."
new7b = "5. submit_plan_to_snow(plan) " + DASH + " one call per plan; for multiple plans, one call per plan in sequential turns (see MULTI-PLAN BATCHING DISCIPLINE above)."
edits.append(("Line 391 — WORKED FLOW step 5", old7b, new7b))

# ---- Change 8: JUSTIFY THE GAP + SLOT ACCOUNTING (line 146 area) ----
# JUSTIFY THE GAP paragraph
old8a = ("JUSTIFY THE GAP " + DASH + " when you submit fewer than 4 plans, name in your "
         "reasoning why no additional valid scenario exists. \"Only one direction reads "
         "cleanly here; the other side has no structural confluence.\" \"I considered a "
         "second BUY at 4530 but the level is outside session ATR.\" \"The existing plan "
         "already covers both timeframes I'd want to trade in this regime.\" When you "
         "submit ZERO new plans because one is already active, name what alternative "
         "scenario you considered and why it doesn't merit its own plan. This forces "
         "canvassing for second-best scenarios rather than stopping at the first thing "
         "you see.")
new8a = ("THERE IS NO QUOTA " + DASH + " name what alternative scenarios you considered, "
         "but submitting fewer plans is fine when fewer scenarios qualify. The 4-plan "
         "ceiling is a CAP, not a target. \"Only one direction reads cleanly here; the "
         "other side has no structural confluence\" is a complete justification " + DASH +
         " no need to manufacture a second-best scenario to fill space. The discipline "
         "is canvassing the chart honestly; the output of that canvassing might be one "
         "plan, three plans, or zero. WAIT with no plans submitted is a valid cycle "
         "outcome when nothing meets your bar.")
edits.append(("Line 146 — JUSTIFY THE GAP softened", old8a, new8a))

# SLOT ACCOUNTING paragraph
old8b = ("SLOT ACCOUNTING " + DASH + " every cycle where total active plans < 4, your "
         "submit_decision reasoning MUST include an explicit slot ledger. Format:\n\n"
         "  Plans active: N/4.\n"
         "  Slot 2 empty: [reason " + DASH + " what scenario you considered and why it "
         "didn't qualify].\n"
         "  Slot 3 empty: [reason].\n"
         "  Slot 4 empty: [reason].\n\n"
         "This is not optional and not satisfied by a single sentence covering \"all the "
         "rest.\" Each empty slot needs its own line because each represents a distinct "
         "scenario you canvassed and rejected " + DASH + " different direction, different "
         "level, different timeframe, different setup_type. \"Slot 2 empty: no "
         "countertrend BUY because the M15 has no bullish reversal structure yet, and a "
         "BUY at 4500 H4 demand sits outside session ATR (45 pips below current).\" "
         "\"Slot 3 empty: a second SELL would need a higher resistance band; the next one "
         "above PLAN-011's 4553 is 4589 H1 and price has already broken below it, so no "
         "fade setup remains.\" \"Slot 4 empty: divergence-play / news-reaction setups "
         "require Echo or rex_divergence_scan signals that aren't present this cycle.\" "
         "If you genuinely cannot articulate three distinct empty-slot rationales, you "
         "haven't canvassed enough " + DASH + " go back to the chart and find the "
         "second-best, third-best, fourth-best scenarios you initially dismissed.")
new8b = ("SLOT NOTES (OPTIONAL) " + DASH + " when fewer than 4 plans are active, you may "
         "include a brief note in your reasoning about scenarios you considered and "
         "passed on. This is for your own audit trail, not a quota mechanism. Examples: "
         "\"Considered a second BUY at 4530 but the level is outside session ATR.\" "
         "\"Considered a SELL fade at 4600 but the resistance lacks confluence with "
         "anything else.\" \"No countertrend setup canvassed: HTF stack and momentum "
         "agree.\" One sentence is fine. Skipping the note is also fine. The honest "
         "answer might be \"only one scenario read cleanly this cycle\" " + DASH + " "
         "submit that one plan and move on.")
edits.append(("Line 152 area — SLOT ACCOUNTING softened", old8b, new8b))

# Apply all edits
print(f'About to apply {len(edits)} edits.\n')
for label, old, new in edits:
    if old not in text:
        print(f'FAIL: {label}')
        print(f'  needle not found. First 80 chars of needle:')
        print(f'  {old[:80]!r}')
        sys.exit(1)
    text = text.replace(old, new)
    print(f'OK:   {label}')

with io.open(PATH, 'w', encoding='utf-8', newline='') as f:
    f.write(text)
print(f'\n{PATH} written successfully.')

# Verification greps
print('\n=== Verification: zero matches expected ===')
for needle in ('paired plans', 'do not hesitate', 'both breakout directions', 'genuinely bidirectional'):
    n = text.lower().count(needle.lower())
    print(f'  "{needle}": {n} matches  {"OK" if n == 0 else "FAIL"}')
