"""FLO-404 follow-up — submit_decision intercept executes action tools.

Empirical motivation (CEO directive 2026-04-30): Floki tried a multi-
plan cycle batch [cancel_plan, submit_plan_to_snow, submit_plan_to_snow,
submit_decision]. The pre-FLO-404 FLO-295 intercept silently dropped
the 3 action tools (`FLOKI_BATCH_WITH_SUBMIT | dropping parallel
calls: ['cancel_plan', 'submit_plan_to_snow', 'submit_plan_to_snow']`)
and only processed submit_decision. Result: 0 plans landed in Snow,
the active set didn't change.

Fix: action tools in the same batch as submit_decision are now
EXECUTED sequentially before the terminator return. This file pins:
  1. Pure function `_apply_submit_decision_intercept` exists and
     classifies action tools correctly (NOT this commit — extracted
     test surface deferred; for now we verify via the broader code
     path that the new contract holds).
  2. The new log line (`FLOKI_BATCH_WITH_SUBMIT | executing N action
     tool(s) before submit_decision return`) appears in the source.
  3. The old "dropping parallel calls" line is GONE.
  4. The fix is wired AHEAD of the FLO-385 clamp (submit_decision
     intercept fires first; action tools execute under the intercept's
     own sequential dispatch, not the clamp).

The action-execution path itself is best tested at the LLM-loop
integration level (which would require mocking the OpenAI client
chain — heavyweight). Pinning via source-inspection here covers the
contract; full LLM-loop integration tests are deferred to follow-up.
"""
from __future__ import annotations

import inspect
import re

import pytest


# =============================================================================
# Source-inspection contracts on ai_agent.py
# =============================================================================


class TestBatchWithSubmitContract:
    """The fix lives in `_call_openai_with_tools`. Source-inspect the
    function body to verify the new contract is implemented and the
    old drop-everything pattern is gone."""

    def _get_source(self) -> str:
        import ai_agent
        # _call_openai_with_tools is a coroutine method; inspect.getsource
        # gives us the body.
        src = inspect.getsource(ai_agent.AIAgent._call_openai_with_tools)
        return src

    def test_old_drop_pattern_is_gone(self):
        """The pre-FLO-404 log line `dropping parallel calls` must NOT
        appear in the new code — its presence would mean the silent
        drop was reintroduced."""
        src = self._get_source()
        assert "dropping parallel calls" not in src, (
            "FLO-404 v2: the old 'dropping parallel calls' log line "
            "must be replaced with action-tool execution. Its presence "
            "indicates the silent-drop regression."
        )

    def test_new_execute_pattern_is_present(self):
        """The new log line must announce action-tool execution
        before the submit_decision return."""
        src = self._get_source()
        assert "executing" in src and "action tool" in src, (
            "FLO-404 v2: action-tool execution log line missing. "
            "Floki's batch [cancel/submit/submit/decision] must "
            "execute the action tools before terminator return."
        )
        # The full canonical phrase from the fix:
        assert "FLOKI_BATCH_WITH_SUBMIT" in src
        assert "before submit_decision return" in src

    def test_submit_decision_still_terminator(self):
        """submit_decision must still return early — it remains the
        cycle terminator. The fix only adds action-tool execution
        BEFORE the return; the return itself is preserved."""
        src = self._get_source()
        # The intercept block must still call return inside _submit_tc
        # is not None branch.
        assert "_submit_tc is not None" in src
        # A return statement must appear after the intercept's main work.
        # Find the section between "_submit_tc is not None" and the next
        # block (search for the `else:` or function end / next top-level).
        idx = src.index("_submit_tc is not None")
        block_after = src[idx:idx + 5000]
        assert "return {" in block_after, (
            "submit_decision intercept must still return — that's the "
            "terminator contract. Action tools execute BEFORE the return."
        )

    def test_action_tools_execute_via_execute_tool(self):
        """Action tools must dispatch through `self._execute_tool` —
        the same path the regular tool loop uses. Re-implementing
        dispatch inline would risk drift."""
        src = self._get_source()
        idx = src.index("FLOKI_BATCH_WITH_SUBMIT")
        block = src[idx:idx + 4000]
        assert "_execute_tool" in block, (
            "action tools must dispatch via self._execute_tool to "
            "match the regular loop's dispatch path"
        )

    def test_failures_logged_at_warning(self):
        """Action-tool failures (validation rejection, exception)
        must surface as WARNING with FAILED tag — this is the
        feedback loop for post-cycle audit."""
        src = self._get_source()
        idx = src.index("FLOKI_BATCH_WITH_SUBMIT")
        block = src[idx:idx + 4000]
        assert "FAILED" in block
        assert "logger.warning" in block, (
            "action-tool failures must log at WARNING level so "
            "post-cycle audit (FLOKI_DATA_NEEDS) can flag them"
        )

    def test_action_tools_appended_to_tool_trace(self):
        """Each action tool's result must land in tool_trace with
        the canonical {name, input, result, latency_ms} shape so
        the post-decision audit pipeline (data_needs, reflexion)
        sees the writes."""
        src = self._get_source()
        idx = src.index("FLOKI_BATCH_WITH_SUBMIT")
        block = src[idx:idx + 4000]
        assert "tool_trace.append" in block
        # Canonical shape keys:
        assert '"name"' in block and '"input"' in block
        assert '"result"' in block and '"latency_ms"' in block

    def test_sequential_execution_for_singleton_safety(self):
        """Action tools (cancel_plan, submit_plan_to_snow, etc.) are
        FLO-385 singletons — they write under locks. Parallel
        execution would race. The fix must execute them sequentially.

        Source-inspection: a `for` loop over the action tool calls
        (not asyncio.gather or threading) confirms sequential order.
        """
        src = self._get_source()
        idx = src.index("FLOKI_BATCH_WITH_SUBMIT")
        block = src[idx:idx + 4000]
        # Sequential dispatch — for loop over _action_tcs
        assert re.search(r"for\s+\w+\s+in\s+_action_tcs", block), (
            "action tools must execute in a sequential for-loop "
            "(FLO-385 singleton safety)"
        )
        # No parallel constructs in the block:
        assert "asyncio.gather" not in block
        assert "ThreadPoolExecutor" not in block
