"""FLO-443 — self-consistency voter on Floki's plan analysis.

Runs N parallel Anthropic API calls (Sonnet, temperature 0.7) over a
compact summary of Floki's analysis, asks each for a directional vote,
and returns a consensus result.

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

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional

try:
    from anthropic import Anthropic
except Exception:  # pragma: no cover
    Anthropic = None  # type: ignore


_DEFAULT_MODEL = "claude-sonnet-4-6"
_N_VOTES = 5
_TEMPERATURE = 0.7
_MAX_TOKENS = 200  # short vote response
_TIMEOUT_SECS = 20.0


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
    confidence_pct: int  # 0-100; share of votes for the winning side
    votes: List[Vote]
    plan_direction: str
    plan_confidence: int
    agreed_with_plan: bool
    elapsed_ms: int
    degraded: bool
    degraded_reason: Optional[str] = None

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


def _one_vote(client, model: str, summary: str) -> Vote:
    try:
        msg = client.messages.create(
            model=model,
            max_tokens=_MAX_TOKENS,
            temperature=_TEMPERATURE,
            messages=[{"role": "user", "content": _VOTE_PROMPT.format(summary=summary)}],
            timeout=_TIMEOUT_SECS,
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        return _parse_vote(text)
    except Exception as e:
        return Vote(direction="ERROR", confidence=0,
                    reason=f"api_error: {type(e).__name__}", raw=str(e)[:200])


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

    if Anthropic is None:
        return ConsensusResult(
            consensus=plan_direction or "NO_TRADE",
            confidence_pct=plan_conf,
            votes=[], plan_direction=plan_direction, plan_confidence=plan_conf,
            agreed_with_plan=True,
            elapsed_ms=int((time.time() - t0) * 1000),
            degraded=True, degraded_reason="anthropic_sdk_unavailable",
        )

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return ConsensusResult(
            consensus=plan_direction or "NO_TRADE",
            confidence_pct=plan_conf,
            votes=[], plan_direction=plan_direction, plan_confidence=plan_conf,
            agreed_with_plan=True,
            elapsed_ms=int((time.time() - t0) * 1000),
            degraded=True, degraded_reason="no_anthropic_api_key",
        )

    client = Anthropic(api_key=api_key)
    mdl = model or _DEFAULT_MODEL
    summary = _build_summary(plan_dict)

    votes: List[Vote] = []
    with ThreadPoolExecutor(max_workers=n_votes) as ex:
        futs = [ex.submit(_one_vote, client, mdl, summary) for _ in range(n_votes)]
        for f in as_completed(futs, timeout=_TIMEOUT_SECS + 5):
            try:
                votes.append(f.result(timeout=_TIMEOUT_SECS))
            except Exception as ex_err:
                votes.append(Vote(direction="ERROR", confidence=0,
                                  reason=f"future_error: {type(ex_err).__name__}",
                                  raw=str(ex_err)[:200]))

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
    confidence_pct = int(round(win_count / n_votes * 100))

    return ConsensusResult(
        consensus=consensus_label,
        confidence_pct=confidence_pct,
        votes=votes,
        plan_direction=plan_direction,
        plan_confidence=plan_conf,
        agreed_with_plan=(consensus_label == plan_direction),
        elapsed_ms=int((time.time() - t0) * 1000),
        degraded=False,
    )
