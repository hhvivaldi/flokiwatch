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
