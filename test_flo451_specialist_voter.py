"""FLO-451 — multi-specialist voter regression tests.

Standalone (no pytest, no live SDK / no WebSearch). Mocks the SDK orchestration
so the test is deterministic, offline, and free. Covers:
  - aggregation rules (3+ APPROVE / 3+ REJECT / no-majority x0.8 / 3+ ABSTAIN skip)
  - timeout -> ABSTAIN
  - SPECIALIST_VOTE_SHADOW log line shows all 5 voters + would_block
Run: python test_flo451_specialist_voter.py
"""
import asyncio
import self_consistency as sc
from self_consistency import SpecialistVote, SpecialistSpec, _aggregate_specialists


def V(name, vote, conf=7):
    return SpecialistVote(name, vote, conf, "stub", [], "stub")


def test_aggregation():
    # 3+ APPROVE -> APPROVE, capped at planner conf
    r, block, conf, deg, _ = _aggregate_specialists(
        [V("NEWS", "APPROVE", 8), V("MACRO", "APPROVE", 8), V("TECHNICAL", "APPROVE", 8),
         V("SENTIMENT", "REJECT", 4), V("DEVIL", "REJECT", 5)], plan_conf=76)
    assert r == "APPROVE" and block is False and deg is False
    assert conf == min(76, int(round((8+8+8+4+5)/5*10))), conf  # avg active *10, capped
    # 3+ REJECT -> REJECT + would_block
    r, block, conf, deg, _ = _aggregate_specialists(
        [V("NEWS", "REJECT", 6), V("MACRO", "REJECT", 7), V("TECHNICAL", "REJECT", 8),
         V("SENTIMENT", "APPROVE", 5), V("DEVIL", "APPROVE", 5)], plan_conf=76)
    assert r == "REJECT" and block is True, (r, block)
    # No majority (2 APPROVE / 2 REJECT / 1 ABSTAIN) -> proceed, x0.8
    r, block, conf, deg, _ = _aggregate_specialists(
        [V("NEWS", "APPROVE", 8), V("MACRO", "APPROVE", 8), V("TECHNICAL", "REJECT", 8),
         V("SENTIMENT", "REJECT", 8), V("DEVIL", "ABSTAIN", 0)], plan_conf=90)
    assert r == "NO_MAJORITY_PROCEED" and block is False, (r, block)
    assert conf == min(90, int(round(8 * 10 * 0.8))), conf   # avg active 8 -> 80 -> *0.8 = 64
    # 3+ TIMEOUTS -> SKIPPED (condition 7), original conf preserved
    def Vt(name):  # timed-out abstain
        return SpecialistVote(name, "ABSTAIN", 0, "timeout", [], "", timed_out=True)
    def Va(name):  # no-data / freshness abstain (NOT a timeout)
        return SpecialistVote(name, "ABSTAIN", 0, "no_data", [], "")
    r, block, conf, deg, reason = _aggregate_specialists(
        [Vt("NEWS"), Vt("MACRO"), Vt("DEVIL"), V("TECHNICAL", "REJECT", 6),
         V("SENTIMENT", "APPROVE", 7)], plan_conf=76)
    assert r == "SKIPPED" and deg is True and conf == 76 and "timeout" in reason, (r, deg, conf, reason)
    # 3 freshness ABSTAINs (NOT timeouts) + 2 active REJECT -> NOT skipped; the
    # active voters carry it (2 REJECT < 3, so NO_MAJORITY_PROCEED).
    r, block, conf, deg, reason = _aggregate_specialists(
        [Va("NEWS"), Va("SENTIMENT"), Va("DEVIL"), V("TECHNICAL", "REJECT", 7),
         V("MACRO", "REJECT", 8)], plan_conf=76)
    assert r == "NO_MAJORITY_PROCEED" and deg is False, (r, deg, reason)
    print("PASS test_aggregation (APPROVE / REJECT+block / NO_MAJORITY x0.8 / 3-TIMEOUT skip / freshness-abstain no-skip)")


def test_timeout_returns_abstain():
    async def _slow(spec, user_msg, model):
        await asyncio.sleep(5)
        return "VOTE: APPROVE\nCONFIDENCE: 9\nREASONING: x\nEVIDENCE: NONE"
    orig = sc._sdk_specialist_call
    sc._sdk_specialist_call = _slow
    try:
        spec = SpecialistSpec("NEWS", "p", ["WebSearch"], 3, 0.1)  # 0.1s timeout
        vote = asyncio.run(sc._run_specialist(spec, "msg", "claude-sonnet-4-6"))
        assert vote.vote == "ABSTAIN" and vote.reasoning == "timeout", (vote.vote, vote.reasoning)
    finally:
        sc._sdk_specialist_call = orig
    print("PASS test_timeout_returns_abstain")


def test_shadow_log_shows_all_five(monkeypatched=None):
    # Mock the whole orchestration so no SDK is touched.
    five = [V("NEWS", "APPROVE", 8), V("MACRO", "REJECT", 3), V("TECHNICAL", "APPROVE", 7),
            V("SENTIMENT", "APPROVE", 6), V("DEVIL", "REJECT", 5)]
    orig_orch = sc._run_specialist_orchestration
    sc._run_specialist_orchestration = lambda specs, user_msg, as_of, summary, model: list(five)

    import logger
    captured = []
    orig_info = logger.log.info
    logger.log.info = lambda m, *a, **k: captured.append(m)
    try:
        plan = {"entry": {"direction": "BUY"}, "analysis": {"confidence": 80, "thesis": "t"}}
        res = sc.run_specialist_vote(plan, mode="shadow", context={"price": 4530})
    finally:
        sc._run_specialist_orchestration = orig_orch
        logger.log.info = orig_info

    assert res.result == "APPROVE" and res.mode == "shadow", (res.result, res.mode)
    assert res.would_block is False
    assert len(res.votes) == 5
    line = next((m for m in captured if "SPECIALIST_VOTE_SHADOW" in m), None)
    assert line is not None, "no SPECIALIST_VOTE_SHADOW log line emitted"
    assert "would_block=" in line, line
    for name in ("news", "macro", "technical", "sentiment", "devil"):
        assert name in line, f"{name} missing from log line: {line}"
    print("PASS test_shadow_log_shows_all_five")
    print("   LOG:", line)


if __name__ == "__main__":
    test_aggregation()
    test_timeout_returns_abstain()
    test_shadow_log_shows_all_five()
    print("\nALL FLO-451 TESTS PASSED")
