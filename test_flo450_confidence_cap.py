"""FLO-450 — self-consistency confidence cap regression test.

The voter must CONFIRM or LOWER the planner's confidence, never inflate it.
Before FLO-450, a unanimous 5/5 vote stamped confidence=100 regardless of the
planner's own 75-78 self-assessment (4 plans in a row). Now the applied
confidence is min(plan_conf, vote_share_pct).

Standalone (no pytest, no live SDK). Monkeypatches `_run_votes` so the test is
deterministic and offline. Run: python test_flo450_confidence_cap.py
"""
import self_consistency as sc
from self_consistency import Vote, _cap_confidence


def _fake_votes(direction, n=5, conf=8):
    return lambda model, summary, k: [Vote(direction, conf, "stub", "stub") for _ in range(n)]


def test_cap_helper():
    assert _cap_confidence(76, 100) == 76      # unanimous can't inflate above 76
    assert _cap_confidence(76, 60) == 60       # split vote LOWERS below 76
    assert _cap_confidence(76, 76) == 76       # equal -> 76
    assert _cap_confidence(0, 100) == 100      # invalid planner conf -> fall back to share
    assert _cap_confidence(90, 100) == 90      # caps at planner's number, not the ceiling
    print("PASS test_cap_helper")


def test_unanimous_does_not_inflate(monkeypatched=None):
    """Goal condition: original_conf=76 + 5/5 unanimous BUY -> confidence=76 (not 100)."""
    orig = sc._run_votes
    sc._run_votes = _fake_votes("BUY", n=5, conf=8)
    try:
        plan = {"entry": {"direction": "BUY"}, "analysis": {"confidence": 76}}
        r = sc.vote_on_plan(plan, n_votes=5)
        assert r.degraded is False, r.degraded_reason
        assert r.consensus == "BUY"
        assert r.vote_share_pct == 100, r.vote_share_pct          # raw share preserved
        assert r.confidence_pct == 76, r.confidence_pct           # APPLIED = capped at 76
        assert r.agreed_with_plan is True
    finally:
        sc._run_votes = orig
    print("PASS test_unanimous_does_not_inflate (vote_share=100, applied=76)")


def test_split_vote_lowers():
    """3/5 BUY + 2/5 NO_TRADE -> share 60 < planner 76 -> applied 60."""
    orig = sc._run_votes
    def mixed(model, summary, k):
        return [Vote("BUY", 7, "s", "s")] * 3 + [Vote("NO_TRADE", 5, "s", "s")] * 2
    sc._run_votes = mixed
    try:
        plan = {"entry": {"direction": "BUY"}, "analysis": {"confidence": 76}}
        r = sc.vote_on_plan(plan, n_votes=5)
        assert r.consensus == "BUY"
        assert r.vote_share_pct == 60, r.vote_share_pct
        assert r.confidence_pct == 60, r.confidence_pct           # lowered below 76
    finally:
        sc._run_votes = orig
    print("PASS test_split_vote_lowers (vote_share=60, applied=60)")


if __name__ == "__main__":
    test_cap_helper()
    test_unanimous_does_not_inflate()
    test_split_vote_lowers()
    print("\nALL FLO-450 TESTS PASSED")
