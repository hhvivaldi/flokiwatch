"""FLO-443 — self-consistency voter on Floki's plan analysis.

Runs N parallel Sonnet calls over a compact summary of Floki's analysis,
asks each for a directional vote, and returns a consensus result.

FLO-449: the votes route through the Agent SDK (claude-agent-sdk + bundled
Claude Code CLI, subscription auth) with model claude-sonnet-4-6 — the
SAME auth Floki's Opus planner uses, no separate ANTHROPIC_API_KEY. The
original anthropic.Anthropic client path degraded on every call because
the bot has no API key in env; the voter never cast a real vote. NOTE: the
SDK does not expose a temperature knob, so vote variance comes from
Sonnet's default sampling rather than the prior temperature=0.7.

Inversion note: Sonnet voting on Opus output is structurally a variance
reduction technique (Wang et al. — "Self-Consistency Improves Chain-of-
Thought Reasoning"), not a second-opinion override. Each vote sees the
*same* Opus-produced summary; the 5-way ensemble at temp 0.7 narrows
the variance in the classification of that summary. It's NOT a weaker
model overriding a stronger model's reasoning — Floki's reasoning is
fixed input.

Env-gated. OFF by default; flip `FLO443_SELF_CONSISTENCY=on` to enable.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# FLO-449: vote through the same Agent SDK path Floki uses (FLO-426) —
# claude-agent-sdk + the bundled Claude Code CLI billed against the Max
# subscription pool. NO separate ANTHROPIC_API_KEY needed. The previous
# anthropic.Anthropic client path degraded on EVERY call (reason=
# no_anthropic_api_key) because the bot runs on subscription auth with no
# key in env, so the voter never cast a single real vote in production.
try:
    import claude_agent_sdk as _claude_agent_sdk  # noqa: F401
    _SDK_AVAILABLE = True
except Exception:  # pragma: no cover
    _SDK_AVAILABLE = False


_DEFAULT_MODEL = "claude-sonnet-4-6"  # lighter than Floki's Opus; same subscription
_N_VOTES = 5
_MAX_TOKENS = 200  # short vote response (advisory; SDK manages output length)
# SDK spawns a Claude Code CLI subprocess per vote — slower to start than a
# direct HTTP call, so the budget is wider than the old direct-API 20s.
_TIMEOUT_SECS = 45.0


def is_enabled() -> bool:
    return os.environ.get("FLO443_SELF_CONSISTENCY", "").strip().lower() in (
        "1", "on", "true", "yes",
    )


@dataclass
class Vote:
    direction: str   # "BUY" / "SELL" / "NO_TRADE" / "ERROR"
    confidence: int  # 1-10
    reason: str
    raw: str


@dataclass
class ConsensusResult:
    consensus: str   # "BUY" / "SELL" / "NO_TRADE" / "DISAGREE"
    confidence_pct: int  # FLO-450: APPLIED confidence = min(planner conf, vote share)
    votes: List[Vote]
    plan_direction: str
    plan_confidence: int
    agreed_with_plan: bool
    elapsed_ms: int
    degraded: bool
    degraded_reason: Optional[str] = None
    vote_share_pct: int = 0  # FLO-450: raw % of votes for winner (pre-cap, for logs)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["votes"] = [asdict(v) for v in self.votes]
        return d


def _build_summary(plan_dict: Dict[str, Any]) -> str:
    """Compact analysis summary for the voter — keep under ~1500 tokens.

    Reads plan.analysis (thesis, key_levels, regime_assumed, setup_type,
    confidence_reason, confidence) + entry (direction, conditions, SL,
    TP, entry_price). Returns a plain-text block.
    """
    a = plan_dict.get("analysis", {}) or {}
    e = plan_dict.get("entry", {}) or {}

    parts = []
    parts.append(f"Plan direction: {e.get('direction', '?')}")
    parts.append(f"Entry price:    {e.get('entry_price', '?')}")
    parts.append(f"Initial SL:     {e.get('initial_sl', '?')}")
    parts.append(f"Initial TP:     {e.get('initial_tp', '?')}")
    parts.append(f"Setup type:     {a.get('setup_type', '?')}")
    parts.append(f"Regime assumed: {a.get('regime_assumed', '?')}")
    parts.append(f"Floki confidence: {a.get('confidence', '?')}")
    parts.append("")
    parts.append("Thesis:")
    parts.append(str(a.get("thesis", ""))[:1200])
    parts.append("")
    parts.append("Confidence reason:")
    parts.append(str(a.get("confidence_reason", ""))[:600])
    parts.append("")
    if a.get("key_levels"):
        parts.append(f"Key levels: {a.get('key_levels')}")
    if e.get("conditions"):
        parts.append(f"Entry conditions: {json.dumps(e.get('conditions'), default=str)[:400]}")
    return "\n".join(parts)


_VOTE_PROMPT = """You are reviewing a single XAU/USD trade plan written by another trader. \
The plan's reasoning is complete and shown below. Your job is NOT to redo the analysis. \
Your job is to read what the trader concluded and judge whether the plan's logic supports \
the plan's stated direction.

Respond in EXACTLY this format:
DIRECTION: <BUY|SELL|NO_TRADE>
CONFIDENCE: <1-10 integer>
REASON: <one short sentence>

Rules:
- DIRECTION must be BUY, SELL, or NO_TRADE. If the analysis is incoherent or contradicts the \
plan's direction, output NO_TRADE.
- CONFIDENCE is your confidence in your DIRECTION choice (not the plan's confidence).
- REASON in one sentence, max 25 words.

PLAN ANALYSIS:
---
{summary}
---

Your verdict:"""


_VOTE_RE = re.compile(
    r"DIRECTION:\s*(BUY|SELL|NO_TRADE)\s*\n+CONFIDENCE:\s*(\d{1,2})\s*\n+REASON:\s*(.+?)(?:\n|$)",
    re.IGNORECASE | re.DOTALL,
)


def _parse_vote(text: str) -> Vote:
    m = _VOTE_RE.search(text or "")
    if m:
        direction = m.group(1).upper()
        try:
            conf = max(1, min(10, int(m.group(2))))
        except Exception:
            conf = 5
        reason = m.group(3).strip()[:200]
        return Vote(direction=direction, confidence=conf, reason=reason, raw=text[:500])
    return Vote(direction="ERROR", confidence=0, reason="parse_failed", raw=text[:500])


async def _sdk_query_text(prompt: str, model: str) -> str:
    """One-shot text query through the Agent SDK (subscription auth).

    Mirrors the FLO-426 Floki path: bundled Claude Code CLI billed against
    the Max subscription pool. `env={"ANTHROPIC_API_KEY": ""}` forces
    subscription auth even if a key leaks into the parent env;
    `setting_sources=[]` strips Claude Code's project scaffolding so the
    voter is a clean LLM call (no MCP tools, no filesystem context).
    """
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        TextBlock,
    )

    options = ClaudeAgentOptions(
        model=model,
        setting_sources=[],
        env={"ANTHROPIC_API_KEY": ""},
        max_turns=1,
    )
    parts: List[str] = []
    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock):
                        parts.append(b.text)
    return "".join(parts)


def _run_votes(model: str, summary: str, n: int) -> List[Vote]:
    """Run `n` votes concurrently in ONE dedicated event loop.

    MUST be invoked from a worker thread (never the SDK's event-loop
    thread): `submit_plan_to_snow` runs inside the FLO-426 SDK event loop
    (tools are dispatched there, see floki_agent_sdk_path._wrapped), so
    `run_until_complete` cannot be called on that thread. A fresh worker
    thread has no running loop. On Windows the default policy hands back a
    Proactor loop (subprocess-capable) — required for the CLI spawn.
    """
    prompt = _VOTE_PROMPT.format(summary=summary)

    async def _gather():
        return await asyncio.gather(
            *[_sdk_query_text(prompt, model) for _ in range(n)],
            return_exceptions=True,
        )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        raw = loop.run_until_complete(_gather())
    finally:
        loop.close()
        asyncio.set_event_loop(None)

    votes: List[Vote] = []
    for r in raw:
        if isinstance(r, BaseException):
            votes.append(Vote(direction="ERROR", confidence=0,
                              reason=f"sdk_error: {type(r).__name__}",
                              raw=str(r)[:200]))
        else:
            votes.append(_parse_vote(r))
    return votes


def _cap_confidence(plan_conf: int, vote_share_pct: int) -> int:
    """FLO-450 — the voter may CONFIRM or LOWER the planner's confidence, never
    inflate it above the planner's own number. Returns min(plan_conf,
    vote_share_pct). If the planner's confidence is missing/invalid (<= 0),
    fall back to the raw vote share (nothing valid to cap against).

    Motivation: 4 consecutive plans (two SELL, two BUY, different regimes) were
    stamped confidence=100 by unanimous 5/5 votes regardless of the planner's
    own 75-78 self-assessment — the mutation had stopped discriminating and
    inflated every plan to the ceiling.
    """
    if plan_conf and plan_conf > 0:
        return min(plan_conf, vote_share_pct)
    return vote_share_pct


def vote_on_plan(plan_dict: Dict[str, Any], *, model: Optional[str] = None,
                 n_votes: int = _N_VOTES) -> ConsensusResult:
    """Run N parallel votes and return a ConsensusResult.

    Fail-soft: any API/parse error counts as one "ERROR" vote rather
    than raising. If ≥ ceil(N/2) votes are ERROR, returns degraded=True
    so the caller can fall through to plan's original confidence.
    """
    t0 = time.time()
    a = plan_dict.get("analysis", {}) or {}
    e = plan_dict.get("entry", {}) or {}
    plan_direction = str(e.get("direction") or "").upper()
    plan_conf_raw = a.get("confidence")
    try:
        plan_conf = int(plan_conf_raw) if plan_conf_raw is not None else 0
    except Exception:
        plan_conf = 0

    if not _SDK_AVAILABLE:
        return ConsensusResult(
            consensus=plan_direction or "NO_TRADE",
            confidence_pct=plan_conf,
            votes=[], plan_direction=plan_direction, plan_confidence=plan_conf,
            agreed_with_plan=True,
            elapsed_ms=int((time.time() - t0) * 1000),
            degraded=True, degraded_reason="agent_sdk_unavailable",
        )

    mdl = model or _DEFAULT_MODEL
    summary = _build_summary(plan_dict)

    # vote_on_plan runs inside the SDK event-loop thread (FLO-426 tool
    # dispatch); offload the 5-way concurrent SDK run to ONE worker thread
    # so its event loop is independent of the planner's loop.
    votes: List[Vote] = []
    try:
        with ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(_run_votes, mdl, summary, n_votes)
            votes = fut.result(timeout=_TIMEOUT_SECS + 15)
    except Exception as ex_err:
        votes = [
            Vote(direction="ERROR", confidence=0,
                 reason=f"vote_runner_error: {type(ex_err).__name__}",
                 raw=str(ex_err)[:200])
            for _ in range(n_votes)
        ]

    # Tally
    counts: Dict[str, int] = {}
    for v in votes:
        counts[v.direction] = counts.get(v.direction, 0) + 1
    error_votes = counts.get("ERROR", 0)
    valid_votes = n_votes - error_votes

    if error_votes >= (n_votes + 1) // 2 or valid_votes == 0:
        return ConsensusResult(
            consensus=plan_direction or "NO_TRADE",
            confidence_pct=plan_conf,
            votes=votes, plan_direction=plan_direction, plan_confidence=plan_conf,
            agreed_with_plan=True,
            elapsed_ms=int((time.time() - t0) * 1000),
            degraded=True,
            degraded_reason=f"too_many_error_votes ({error_votes}/{n_votes})",
        )

    # Drop ERROR for consensus determination
    non_error = {k: v for k, v in counts.items() if k != "ERROR"}
    winner = max(non_error.items(), key=lambda kv: kv[1])
    win_direction, win_count = winner

    # Disagreement check: if no direction has > 50% of valid votes
    # (i.e. it's tied 2-2 with NO_TRADE as the 5th, etc.), call it
    # DISAGREE so the caller can reject the plan.
    consensus_label = win_direction if win_count > valid_votes / 2 else "DISAGREE"
    vote_share_pct = int(round(win_count / n_votes * 100))
    # FLO-450: cap the applied confidence at the planner's own number — the
    # voter confirms or lowers, never inflates. (See _cap_confidence.)
    applied_confidence = _cap_confidence(plan_conf, vote_share_pct)

    return ConsensusResult(
        consensus=consensus_label,
        confidence_pct=applied_confidence,
        vote_share_pct=vote_share_pct,
        votes=votes,
        plan_direction=plan_direction,
        plan_confidence=plan_conf,
        agreed_with_plan=(consensus_label == plan_direction),
        elapsed_ms=int((time.time() - t0) * 1000),
        degraded=False,
    )


# =============================================================================
# FLO-451 — Multi-Specialist Voter
# =============================================================================
# Replaces the 5 uniform Sonnet voters (FLO-443/450) with 5 SPECIALISTS, each
# with a distinct system prompt + data dimension. Voters 1-4 (News, Macro,
# Technical, Sentiment) run in parallel; the Devil's Advocate runs AFTER, seeing
# their outputs. Aggregation is pure code. Behaviour is gated by
# FLO451_VOTER_MODE: shadow (log only, plan proceeds) | confidence (mutate conf)
# | block (3+ REJECT blocks). Default: shadow.
#
# WebSearch max_uses / allowed_domains: the subscription Agent SDK exposes no
# Messages-API web_search knobs (verified, SDK 0.1.81 — ClaudeAgentOptions has
# allowed_tools but no max_uses/allowed_domains), so the per-voter search cap and
# domain allowlist are PROMPT-enforced (stated in each system prompt). The
# technical voter gets allowed_tools=[] (hard 0). Hard enforcement via a
# can_use_tool callback is a follow-up if shadow data shows prompt drift.

_VOTER_MODE_ENV = "FLO451_VOTER_MODE"
_VALID_MODES = ("shadow", "confidence", "block")
# FLO-451 live-run tuning (2026-05-21): web-searching voters need room for the
# search round-trip + reasoning. First live run timed out MACRO (was 45s) and
# DEVIL (was 30s) while NEWS/SENTIMENT finished ~45s. Bumped so a web voter can
# complete a couple of searches before ABSTAIN-on-timeout.
_SPECIALIST_TIMEOUT_SECS = 75.0
_DEVIL_TIMEOUT_SECS = 60.0
_ORCHESTRATION_TIMEOUT_SECS = 180.0

_NEWS_DOMAINS = "reuters.com, apnews.com, cnbc.com, kitco.com, fxstreet.com"
_MACRO_DOMAINS = "fred.stlouisfed.org, tradingeconomics.com, cnbc.com"
_SENTIMENT_DOMAINS = "cftc.gov, etf.com, dailyfx.com"


def voter_mode() -> str:
    """FLO-451 behaviour switch. shadow (default) | confidence | block."""
    m = os.environ.get(_VOTER_MODE_ENV, "shadow").strip().lower()
    return m if m in _VALID_MODES else "shadow"


@dataclass
class SpecialistSpec:
    name: str
    system_prompt: str
    allowed_tools: List[str]
    web_search_max_uses: int
    timeout_secs: float


@dataclass
class SpecialistVote:
    name: str
    vote: str            # APPROVE | REJECT | ABSTAIN
    confidence: int      # 1-10 (0 when ABSTAIN)
    reasoning: str
    evidence: List[Dict[str, str]]
    raw: str
    timed_out: bool = False   # FLO-451: distinguishes timeout from NO_DATA abstain


@dataclass
class SpecialistResult:
    result: str          # APPROVE | REJECT | NO_MAJORITY_PROCEED | SKIPPED
    mode: str
    would_block: bool
    applied_confidence: int
    plan_direction: str
    plan_confidence: int
    votes: List[SpecialistVote]
    elapsed_ms: int
    degraded: bool
    degraded_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["votes"] = [asdict(v) for v in self.votes]
        return d


_OUTPUT_CONTRACT = (
    "Respond in EXACTLY this format and nothing else:\n"
    "VOTE: <APPROVE|REJECT|NO_DATA>\n"
    "CONFIDENCE: <integer 1-10>\n"
    "REASONING: <one sentence, max 30 words>\n"
    "EVIDENCE: <source | ISO-8601 timestamp>; <source | ts>   (or NONE)"
)


def _freshness_preamble(as_of_iso: str, max_searches: int, domains: str) -> str:
    return (
        f"Today is {as_of_iso} (UTC). Only cite sources from today / the last 24 hours. "
        f"Include the publication date/time of every source you cite. "
        f"If you cannot find fresh data, vote NO_DATA instead of guessing. "
        f"You may perform at most {max_searches} web searches this session. "
        f"Only cite results from these domains: {domains}."
    )


def _news_prompt(as_of_iso: str) -> str:
    return (
        "You are a gold (XAU/USD) NEWS & GEOPOLITICS analyst reviewing a trade plan.\n"
        + _freshness_preamble(as_of_iso, 3, _NEWS_DOMAINS) + "\n"
        "Question: Are there news events TODAY that SUPPORT or CONTRADICT this plan's "
        "direction (rate decisions, CPI/NFP, geopolitical risk, central-bank gold buying)?\n"
        "Vote APPROVE if today's news supports the plan's direction, REJECT if it "
        "contradicts it, NO_DATA if you cannot find today's news.\n" + _OUTPUT_CONTRACT
    )


def _macro_prompt(as_of_iso: str) -> str:
    return (
        "You are a MACRO analyst reviewing a gold (XAU/USD) trade plan. Pre-fetched DXY "
        "and Luna macro context is in the user message.\n"
        + _freshness_preamble(as_of_iso, 2, _MACRO_DOMAINS) + "\n"
        "Question: Is the macro environment (DXY, US treasury yields, VIX) aligned WITH "
        "this gold trade? Gold is inversely correlated with the US dollar and real yields "
        "(rising DXY/yields is a headwind for long gold).\n"
        "Vote APPROVE if macro supports the trade direction, REJECT if macro fights it.\n"
        + _OUTPUT_CONTRACT
    )


def _technical_prompt(as_of_iso: str) -> str:
    # Condition 3: must contain 'EMA50' and 'above or below' (also EMA200).
    return (
        "You are a HIGHER-TIMEFRAME TECHNICAL analyst reviewing a gold (XAU/USD) trade "
        "plan. Do NOT use web search. All data you need (multi-timeframe indicators "
        "computed by the system) is provided in the user message.\n"
        "Question: Is price above or below the D1 and H4 EMA50 and EMA200? Is the EMA "
        "stack aligned with this trade's direction? Is this trade WITH or AGAINST the "
        "higher-timeframe trend?\n"
        "A BUY below a falling D1/H4 EMA50/EMA200 stack is counter-trend and must be "
        "challenged; a SELL into a rising stack likewise.\n"
        "Vote APPROVE if the HTF structure supports the trade direction, REJECT if the "
        "trade fights the HTF EMA stack.\n" + _OUTPUT_CONTRACT
    )


def _sentiment_prompt(as_of_iso: str) -> str:
    return (
        "You are a SENTIMENT & POSITIONING analyst reviewing a gold (XAU/USD) trade plan.\n"
        + _freshness_preamble(as_of_iso, 3, _SENTIMENT_DOMAINS) + "\n"
        "Question: Are institutional traders (CFTC COT report, GLD/ETF flows, futures open "
        "interest) positioned WITH or AGAINST this trade? Is retail crowded on one side?\n"
        "Vote APPROVE if positioning supports the trade, REJECT if positioning is against "
        "it or dangerously crowded.\n" + _OUTPUT_CONTRACT
    )


def _devil_prompt(as_of_iso: str) -> str:
    # Condition 5: must contain 'weakest assumptions'.
    return (
        "You are the DEVIL'S ADVOCATE reviewing a gold (XAU/USD) trade plan AND the four "
        "specialist opinions in the user message. Be skeptical.\n"
        f"Today is {as_of_iso} (UTC). You may perform at most 1 web search to verify a "
        "single specific doubt; cite its publication date.\n"
        "Question: What are the 3 weakest assumptions in this plan? Would you risk $50 of "
        "your own money on it — why or why not?\n"
        "Vote REJECT if the plan's weakest assumptions are likely to break; APPROVE only "
        "if it survives scrutiny.\n" + _OUTPUT_CONTRACT
    )


def _build_specialist_specs(as_of_iso: str) -> List[SpecialistSpec]:
    """The 4 parallel specialists (Devil's Advocate is built separately because
    it needs the other four's outputs)."""
    return [
        SpecialistSpec("NEWS", _news_prompt(as_of_iso), ["WebSearch"], 3, _SPECIALIST_TIMEOUT_SECS),
        SpecialistSpec("MACRO", _macro_prompt(as_of_iso), ["WebSearch"], 2, _SPECIALIST_TIMEOUT_SECS),
        SpecialistSpec("TECHNICAL", _technical_prompt(as_of_iso), [], 0, _SPECIALIST_TIMEOUT_SECS),
        SpecialistSpec("SENTIMENT", _sentiment_prompt(as_of_iso), ["WebSearch"], 3, _SPECIALIST_TIMEOUT_SECS),
    ]


def _compact_htf(mtf: Any) -> str:
    """FLO-451 — serialize multi-TF indicators HTF-FIRST so the Technical voter
    always sees D1/H4/H1 (the live-run bug: a flat str()[:1500] truncated after
    M1/M5 and starved the flagship voter of higher-timeframe EMAs)."""
    try:
        ind = (mtf or {}).get("multi_tf_indicators") or {}
        reg = (mtf or {}).get("market_regime")
        lines: List[str] = []
        if reg:
            lines.append(f"market_regime: {json.dumps(reg, default=str)[:300]}")
        for tf in ("D1", "H4", "H1", "M30", "M15", "M5", "M1"):
            if isinstance(ind, dict) and tf in ind:
                lines.append(f"{tf}: {json.dumps(ind[tf], default=str)[:450]}")
        return "\n".join(lines)[:3500] if lines else str(mtf)[:2000]
    except Exception:
        return str(mtf)[:2000]


def _build_specialist_user_msg(summary: str, context: Optional[Dict[str, Any]],
                               as_of_iso: str) -> str:
    ctx = context or {}
    parts = [f"AS OF: {as_of_iso} UTC", ""]
    if ctx.get("price") is not None:
        parts.append(f"Current XAU/USD price: {ctx.get('price')}")
    parts += ["", "PLAN UNDER REVIEW:", summary, ""]
    if ctx.get("multi_tf"):
        parts += ["MULTI-TIMEFRAME INDICATORS (for the TECHNICAL voter — HTF first):",
                  _compact_htf(ctx.get("multi_tf")), ""]
    if ctx.get("dxy"):
        parts += ["DXY / MACRO (for the MACRO voter):", str(ctx.get("dxy"))[:800], ""]
    if ctx.get("luna"):
        parts += ["LUNA MACRO BRIEF (for the MACRO voter):", str(ctx.get("luna"))[:1200], ""]
    if ctx.get("echo"):
        parts += ["ECHO NEWS ALERTS (for the NEWS voter):", str(ctx.get("echo"))[:1200], ""]
    return "\n".join(parts)


def _build_devil(as_of_iso: str, summary: str, votes: List[SpecialistVote]) -> tuple:
    spec = SpecialistSpec("DEVIL", _devil_prompt(as_of_iso), ["WebSearch"], 1, _DEVIL_TIMEOUT_SECS)
    op = "\n".join(
        f"- {v.name}: {v.vote} (conf {v.confidence}) — {v.reasoning}" for v in votes
    )
    user_msg = (
        f"AS OF: {as_of_iso} UTC\n\nPLAN UNDER REVIEW:\n{summary}\n\n"
        f"FOUR SPECIALIST OPINIONS:\n{op}\n\nNow give your devil's-advocate verdict."
    )
    return spec, user_msg


_SPEC_RE = re.compile(
    r"VOTE:\s*(APPROVE|REJECT|NO_DATA|ABSTAIN)\s*\n+CONFIDENCE:\s*(\d{1,2})"
    r"\s*\n+REASONING:\s*(.+?)(?:\n|$)",
    re.IGNORECASE | re.DOTALL,
)


def _parse_specialist(name: str, text: str) -> SpecialistVote:
    m = _SPEC_RE.search(text or "")
    if not m:
        return SpecialistVote(name, "ABSTAIN", 0, "parse_failed", [], (text or "")[:300])
    vote = m.group(1).upper()
    if vote in ("NO_DATA", "ABSTAIN"):
        return SpecialistVote(name, "ABSTAIN", 0, m.group(3).strip()[:200], [], (text or "")[:400])
    try:
        conf = max(1, min(10, int(m.group(2))))
    except Exception:
        conf = 5
    return SpecialistVote(name, vote, conf, m.group(3).strip()[:200], [], (text or "")[:400])


async def _sdk_specialist_call(spec: SpecialistSpec, user_msg: str, model: str) -> str:
    """One specialist SDK call. allowed_tools=['WebSearch'] (or [] for technical).
    Subscription auth (no API key). max_turns scales with the search budget so
    tool-using voters have room to search-then-answer."""
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        TextBlock,
    )

    max_turns = 2 if not spec.allowed_tools else (spec.web_search_max_uses + 3)
    options = ClaudeAgentOptions(
        model=model,
        system_prompt=spec.system_prompt,
        allowed_tools=spec.allowed_tools,
        setting_sources=[],
        env={"ANTHROPIC_API_KEY": ""},
        max_turns=max_turns,
    )
    parts: List[str] = []
    async with ClaudeSDKClient(options=options) as client:
        await client.query(user_msg)
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock):
                        parts.append(b.text)
    return "".join(parts)


async def _run_specialist(spec: SpecialistSpec, user_msg: str, model: str) -> SpecialistVote:
    try:
        text = await asyncio.wait_for(
            _sdk_specialist_call(spec, user_msg, model), timeout=spec.timeout_secs
        )
        return _parse_specialist(spec.name, text)
    except asyncio.TimeoutError:
        return SpecialistVote(spec.name, "ABSTAIN", 0, "timeout", [], "", timed_out=True)
    except Exception as e:  # pragma: no cover - SDK/runtime errors
        return SpecialistVote(spec.name, "ABSTAIN", 0, f"error: {type(e).__name__}", [], str(e)[:200])


def _run_specialist_orchestration(specs: List[SpecialistSpec], user_msg: str,
                                  as_of_iso: str, summary: str, model: str) -> List[SpecialistVote]:
    """Worker-thread entry: voters 1-4 in parallel, Devil's Advocate sequentially
    after. Runs in ONE fresh event loop (must NOT be the SDK planner's loop)."""
    async def _go():
        results = await asyncio.gather(
            *[_run_specialist(s, user_msg, model) for s in specs],
            return_exceptions=True,
        )
        votes: List[SpecialistVote] = []
        for s, r in zip(specs, results):
            if isinstance(r, SpecialistVote):
                votes.append(r)
            else:
                votes.append(SpecialistVote(s.name, "ABSTAIN", 0,
                                            f"gather_error: {type(r).__name__}", [], str(r)[:120]))
        devil_spec, devil_msg = _build_devil(as_of_iso, summary, votes)
        votes.append(await _run_specialist(devil_spec, devil_msg, model))
        return votes

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_go())
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def _aggregate_specialists(votes: List[SpecialistVote], plan_conf: int):
    """Pure aggregation. Returns (result, would_block, applied_confidence,
    degraded, reason). 3+ APPROVE -> APPROVE; 3+ REJECT -> REJECT (would_block);
    else NO_MAJORITY_PROCEED (confidence x0.8). 3+ ABSTAIN -> SKIPPED."""
    approve = sum(1 for v in votes if v.vote == "APPROVE")
    reject = sum(1 for v in votes if v.vote == "REJECT")
    timeouts = sum(1 for v in votes if getattr(v, "timed_out", False))
    active = [v for v in votes if v.vote in ("APPROVE", "REJECT")]

    # Per spec (condition 7): 3+ TIMEOUTS skip voting. A NO_DATA / freshness
    # ABSTAIN is a legitimate "no opinion" — it must NOT force a skip; the
    # remaining active voters carry the verdict. (Live-run finding: lumping
    # freshness-abstain with timeout made the ensemble SKIP whenever the web
    # voters found no same-day sources, which is most of the time.)
    if timeouts >= 3 or not active:
        reason = f"too_many_timeouts ({timeouts})" if timeouts >= 3 else "no_active_voters"
        return "SKIPPED", False, plan_conf, True, reason

    avg_active = sum(v.confidence for v in active) / len(active)  # 1-10
    avg_pct = int(round(avg_active * 10))                          # -> 0-100

    if approve >= 3:
        return "APPROVE", False, _cap_confidence(plan_conf, avg_pct), False, None
    if reject >= 3:
        return "REJECT", True, _cap_confidence(plan_conf, avg_pct), False, None
    # No clear majority -> tie goes to the planner (Opus), but lower confidence.
    nm_pct = int(round(avg_active * 10 * 0.8))
    return "NO_MAJORITY_PROCEED", False, _cap_confidence(plan_conf, nm_pct), False, None


def _log_specialist(res: SpecialistResult) -> None:
    try:
        from logger import log
    except Exception:  # pragma: no cover
        return
    token = "SPECIALIST_VOTE_SHADOW" if res.mode == "shadow" else "SPECIALIST_VOTE"
    vsum = " ".join(f"{v.name.lower()}={v.vote}:{v.confidence}" for v in res.votes) or "(no votes)"
    msg = (
        f"{token} | result={res.result} mode={res.mode} would_block={res.would_block} "
        f"applied_conf={res.applied_confidence} plan_conf={res.plan_confidence} | "
        f"{vsum} | elapsed_ms={res.elapsed_ms}"
    )
    if res.degraded:
        msg += f" | DEGRADED {res.degraded_reason}"
    log.info(msg)


def run_specialist_vote(plan_dict: Dict[str, Any], *, context: Optional[Dict[str, Any]] = None,
                        mode: Optional[str] = None,
                        model: Optional[str] = None) -> SpecialistResult:
    """FLO-451 — run the 5-specialist ensemble on a plan and return a
    SpecialistResult. Emits the SPECIALIST_VOTE[_SHADOW] log line itself.

    Fail-soft: SDK unavailable, orchestration error, or 3+ ABSTAIN -> SKIPPED
    (degraded, plan's original confidence preserved). The caller decides what to
    DO with the result per `mode` (shadow/confidence/block).
    """
    t0 = time.time()
    mode = (mode or voter_mode())
    a = plan_dict.get("analysis", {}) or {}
    e = plan_dict.get("entry", {}) or {}
    plan_dir = str(e.get("direction") or "").upper()
    try:
        plan_conf = int(a.get("confidence")) if a.get("confidence") is not None else 0
    except Exception:
        plan_conf = 0

    def _skip(reason: str) -> SpecialistResult:
        r = SpecialistResult("SKIPPED", mode, False, plan_conf, plan_dir, plan_conf, [],
                             int((time.time() - t0) * 1000), True, reason)
        _log_specialist(r)
        return r

    if not _SDK_AVAILABLE:
        return _skip("agent_sdk_unavailable")

    as_of_iso = (context or {}).get("as_of_iso") or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    summary = _build_summary(plan_dict)
    user_msg = _build_specialist_user_msg(summary, context, as_of_iso)
    specs = _build_specialist_specs(as_of_iso)
    mdl = model or _DEFAULT_MODEL

    try:
        with ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(_run_specialist_orchestration, specs, user_msg, as_of_iso, summary, mdl)
            votes = fut.result(timeout=_ORCHESTRATION_TIMEOUT_SECS)
    except Exception as e2:
        return _skip(f"orchestration_error: {type(e2).__name__}")

    result, would_block, applied, degraded, reason = _aggregate_specialists(votes, plan_conf)
    res = SpecialistResult(result, mode, would_block, applied, plan_dir, plan_conf, votes,
                           int((time.time() - t0) * 1000), degraded, reason)
    _log_specialist(res)
    return res
