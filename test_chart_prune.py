"""FLO-420: Image pruning test suite.

Run with `python test_chart_prune.py`. Exits non-zero on failure.

Tests the _apply_chart_prunes helper in ai_agent.py:
  1. Images present at iter N+1 (Floki sees them once)
  2. Images replaced by placeholders at iter N+2
  3. Placeholders contain symbol, timeframe, original timestamp
  4. System+tools cache breakpoint untouched (idx=0 system message)
  5. Context size before/after — placeholder version is dramatically
     smaller (proxy for cache_creation_input_tokens reduction)
"""
from __future__ import annotations

import json
import sys

from ai_agent import _apply_chart_prunes


def assert_eq(actual, expected, label):
    if actual != expected:
        print(f"FAIL [{label}]: expected {expected!r}, got {actual!r}")
        sys.exit(1)
    print(f"PASS [{label}]")


def assert_true(cond, label):
    if not cond:
        print(f"FAIL [{label}]: condition false")
        sys.exit(1)
    print(f"PASS [{label}]")


def _make_image_msg(timeframes, fake_b64="A" * 90000):
    """Mimics the exact shape produced by the deferred image message
    block in ai_agent.py:2697-2705 (text header, then image+label per tf)."""
    blocks = [{"type": "text", "text": "Chart screenshots attached. Analyze candle patterns, S/R interactions, volume bars, and momentum visually:"}]
    labels = {"D1": "Daily", "H4": "4-Hour", "H1": "1-Hour", "M15": "15-Min", "M5": "5-Min", "M1": "1-Min"}
    for tf in timeframes:
        blocks.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{fake_b64}", "detail": "high"}})
        blocks.append({"type": "text", "text": f"Above: XAUUSD {tf} ({labels.get(tf, tf)}) chart."})
    return {"role": "user", "content": blocks}


def _build_messages():
    """Emulate the real loop layout up through iter=2 of a cycle that
    fetched charts at iter=0:
      [0] system           (cache breakpoint)
      [1] user trigger
      [2] assistant tool_calls (iter=0)
      [3] tool result (get_chart_screenshots)
      [4] user image-message  (iter=0 deferred append)   <-- prune target
      [5] assistant tool_calls (iter=1)
      [6] tool result
    """
    return [
        {"role": "system", "content": "SYSTEM PROMPT — large, cache breakpoint here."},
        {"role": "user", "content": "Trigger context for cycle."},
        {"role": "assistant", "content": "iter0 reasoning + tool_calls"},
        {"role": "tool", "tool_call_id": "tc_1", "content": json.dumps({"success": True, "timeframes": ["D1", "H4"]})},
        _make_image_msg(["D1", "H4", "H1", "M15", "M5", "M1"]),
        {"role": "assistant", "content": "iter1 reasoning that references the charts visually"},
        {"role": "tool", "tool_call_id": "tc_2", "content": json.dumps({"success": True})},
    ]


def _content_size_chars(messages):
    """Total chars across all content blocks (proxy for token cost)."""
    total = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            total += len(c)
        elif isinstance(c, list):
            for b in c:
                if isinstance(b, dict):
                    if b.get("type") == "text":
                        total += len(str(b.get("text", "")))
                    elif b.get("type") == "image_url":
                        url = ((b.get("image_url") or {}).get("url") or "")
                        total += len(url)
    return total


def test_1_images_present_at_iter_n_plus_1():
    """At iter=1 (just after charts appended at iter=0), images must
    still be in history — Floki must SEE them once."""
    messages = _build_messages()
    pending = [{
        "iter_appended": 0,
        "msg_index": 4,
        "timeframes": ["D1", "H4", "H1", "M15", "M5", "M1"],
        "timestamp_iso": "2026-05-06T10:00:00Z",
    }]
    # Simulate top-of-loop at iter=1. Condition: 1 >= 0 + 2 → False → no prune.
    pruned = _apply_chart_prunes(messages, pending, iteration=1)
    assert_eq(pruned, [], "test1.no_prune_at_iter1")
    img_blocks = [b for b in messages[4]["content"] if isinstance(b, dict) and b.get("type") == "image_url"]
    assert_eq(len(img_blocks), 6, "test1.six_images_still_present")
    assert_eq(len(pending), 1, "test1.entry_still_pending")


def test_2_images_replaced_at_iter_n_plus_2():
    """At iter=2, condition fires. Image_url blocks gone, only text remains."""
    messages = _build_messages()
    pending = [{
        "iter_appended": 0,
        "msg_index": 4,
        "timeframes": ["D1", "H4", "H1", "M15", "M5", "M1"],
        "timestamp_iso": "2026-05-06T10:00:00Z",
    }]
    pruned = _apply_chart_prunes(messages, pending, iteration=2)
    assert_eq(len(pruned), 1, "test2.one_prune_event")
    assert_eq(pruned[0]["images_pruned"], 6, "test2.six_images_pruned")
    assert_eq(pruned[0]["iter_appended"], 0, "test2.log_iter_appended")
    assert_eq(pruned[0]["iter_pruned"], 2, "test2.log_iter_pruned")
    assert_eq(pruned[0]["msg_index"], 4, "test2.log_msg_index")
    assert_true(pruned[0]["placeholder_est_tokens"] > 0, "test2.token_estimate_positive")
    new_content = messages[4]["content"]
    img_blocks = [b for b in new_content if isinstance(b, dict) and b.get("type") == "image_url"]
    assert_eq(len(img_blocks), 0, "test2.zero_images_after_prune")
    text_blocks = [b for b in new_content if isinstance(b, dict) and b.get("type") == "text"]
    assert_eq(len(text_blocks), 6, "test2.six_text_placeholders")
    assert_eq(pending, [], "test2.pending_drained")


def test_3_placeholder_content():
    """Placeholders carry symbol XAUUSD, timeframe, original timestamp."""
    messages = _build_messages()
    pending = [{
        "iter_appended": 0,
        "msg_index": 4,
        "timeframes": ["D1", "H4", "H1", "M15", "M5", "M1"],
        "timestamp_iso": "2026-05-06T10:00:00Z",
    }]
    _apply_chart_prunes(messages, pending, iteration=2)
    texts = [b["text"] for b in messages[4]["content"]]
    expected = [
        "[chart XAUUSD D1 — shown at 2026-05-06T10:00:00Z, visual analysis incorporated]",
        "[chart XAUUSD H4 — shown at 2026-05-06T10:00:00Z, visual analysis incorporated]",
        "[chart XAUUSD H1 — shown at 2026-05-06T10:00:00Z, visual analysis incorporated]",
        "[chart XAUUSD M15 — shown at 2026-05-06T10:00:00Z, visual analysis incorporated]",
        "[chart XAUUSD M5 — shown at 2026-05-06T10:00:00Z, visual analysis incorporated]",
        "[chart XAUUSD M1 — shown at 2026-05-06T10:00:00Z, visual analysis incorporated]",
    ]
    assert_eq(texts, expected, "test3.placeholder_text_verbatim")


def test_4_system_prompt_untouched():
    """Cache breakpoint at messages[0] must survive pruning."""
    messages = _build_messages()
    system_before = json.dumps(messages[0])
    user_trigger_before = json.dumps(messages[1])
    pending = [{
        "iter_appended": 0,
        "msg_index": 4,
        "timeframes": ["D1", "H4", "H1", "M15", "M5", "M1"],
        "timestamp_iso": "2026-05-06T10:00:00Z",
    }]
    _apply_chart_prunes(messages, pending, iteration=2)
    assert_eq(json.dumps(messages[0]), system_before, "test4.system_msg_unchanged")
    assert_eq(json.dumps(messages[1]), user_trigger_before, "test4.user_trigger_unchanged")


def test_5_context_size_reduction():
    """Before/after content size — proxy for cache_creation reduction.
    Real images would be ~90KB base64 each × 6 = 540KB. Placeholders ~80
    chars × 6 = 480. Reduction must be > 99%."""
    messages_before = _build_messages()
    messages_after = _build_messages()
    pending = [{
        "iter_appended": 0,
        "msg_index": 4,
        "timeframes": ["D1", "H4", "H1", "M15", "M5", "M1"],
        "timestamp_iso": "2026-05-06T10:00:00Z",
    }]
    _apply_chart_prunes(messages_after, pending, iteration=2)
    before = _content_size_chars(messages_before)
    after = _content_size_chars(messages_after)
    pct_reduction = 100.0 * (before - after) / before if before else 0
    print(f"  context_chars_before={before:,}  after={after:,}  reduction={pct_reduction:.2f}%")
    assert_true(pct_reduction > 99.0, "test5.size_reduction_gt_99pct")
    # Per-iter token estimate (chars/4 is wrong for images but stable proxy):
    print(f"  est_tokens_before={before // 4:,}  after={after // 4:,}")
    print(f"  saved_per_iter_tokens={(before - after) // 4:,}")


def test_6_multi_pending_partial_drain():
    """Two pending entries, only the older one drains at iteration=2."""
    messages = [{"role": "system", "content": "sys"}]
    msg_a = _make_image_msg(["D1", "H4"])
    msg_b = _make_image_msg(["M5", "M1"])
    messages.append(msg_a)  # idx=1
    messages.append(msg_b)  # idx=2
    pending = [
        {"iter_appended": 0, "msg_index": 1, "timeframes": ["D1", "H4"],
         "timestamp_iso": "2026-05-06T10:00:00Z"},
        {"iter_appended": 1, "msg_index": 2, "timeframes": ["M5", "M1"],
         "timestamp_iso": "2026-05-06T10:01:00Z"},
    ]
    pruned = _apply_chart_prunes(messages, pending, iteration=2)
    assert_eq(len(pruned), 1, "test6.only_old_entry_pruned")
    assert_eq(len(pending), 1, "test6.new_entry_still_pending")
    assert_eq(pending[0]["iter_appended"], 1, "test6.remaining_is_newer")
    img_a = [b for b in messages[1]["content"] if b.get("type") == "image_url"]
    img_b = [b for b in messages[2]["content"] if b.get("type") == "image_url"]
    assert_eq(len(img_a), 0, "test6.old_pruned")
    assert_eq(len(img_b), 2, "test6.new_kept")


if __name__ == "__main__":
    print("=" * 60)
    print("FLO-420 image-pruning test suite")
    print("=" * 60)
    test_1_images_present_at_iter_n_plus_1()
    test_2_images_replaced_at_iter_n_plus_2()
    test_3_placeholder_content()
    test_4_system_prompt_untouched()
    test_5_context_size_reduction()
    test_6_multi_pending_partial_drain()
    print("=" * 60)
    print("ALL TESTS PASSED")
