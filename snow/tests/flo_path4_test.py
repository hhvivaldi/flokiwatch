"""FLO-Path4 — auto-inject intelligence block + validator min entry conditions.

Two coordinated tests:
  * `_check_min_entry_conditions` accepts >=2, rejects <2
  * `build_intelligence_block` produces Bug-G-safe XML payload from
    Luna brief + filtered Echo alerts, with graceful degradation when
    files are missing.
"""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from snow.validator import validate_plan, _check_min_entry_conditions
from snow.schema import Plan
from agent_data_builder import (
    build_intelligence_block,
    _build_luna_section,
    _build_echo_section,
    _intel_age_minutes,
)


# =============================================================================
# Validator min_entry_conditions
# =============================================================================

class TestMinEntryConditions:
    def test_single_condition_rejected(self, valid_plan_dict):
        valid_plan_dict["entry"]["conditions"] = [
            {"type": "price_above", "level": 4730.0},
        ]
        ok, _, errors = validate_plan(valid_plan_dict)
        assert not ok
        msg = " ".join(errors)
        assert "at least 2 conditions" in msg
        assert "got 1" in msg

    def test_two_conditions_accepted(self, valid_plan_dict):
        # canonical _BASE_PLAN already has 2 conditions; verify happy path.
        ok, _, errors = validate_plan(valid_plan_dict)
        assert ok, errors

    def test_three_conditions_accepted(self, valid_plan_dict):
        valid_plan_dict["entry"]["conditions"] = [
            {"type": "price_above", "level": 4730.0},
            {"type": "rsi", "tf": "H1", "op": "above", "threshold": 70},
            {"type": "macd_histogram", "tf": "H1", "op": "above",
             "threshold": 0.0},
        ]
        ok, _, errors = validate_plan(valid_plan_dict)
        assert ok, errors

    def test_error_message_names_alternatives(self, valid_plan_dict):
        valid_plan_dict["entry"]["conditions"] = [
            {"type": "price_above", "level": 4730.0},
        ]
        ok, _, errors = validate_plan(valid_plan_dict)
        assert not ok
        msg = " ".join(errors).lower()
        # Error message gives Floki concrete revision direction
        assert "indicator" in msg or "structural" in msg or "time" in msg


# =============================================================================
# Intelligence block — Bug G discipline + graceful degradation
# =============================================================================

class TestIntelligenceBlock:
    def test_block_returns_xml_tagged_string(self, tmp_path):
        out = build_intelligence_block(
            luna_path=str(tmp_path / "no_luna.json"),
            echo_path=str(tmp_path / "no_echo.json"),
        )
        assert isinstance(out, str)
        assert out.startswith("<intelligence")
        assert out.rstrip().endswith("</intelligence>")
        assert "framing=\"observational\"" in out

    def test_missing_luna_file_yields_unavailable(self, tmp_path):
        out = build_intelligence_block(
            luna_path=str(tmp_path / "missing.json"),
            echo_path=str(tmp_path / "missing2.json"),
        )
        assert "no_brief_on_disk" in out

    def test_luna_section_strips_internal_fields(self):
        # Bug G discipline: source / error / headlines_consumed must be
        # stripped from auto-injected payload.
        raw_luna = {
            "timestamp": "2026-04-28T16:00:00Z",
            "key_factors": ["Gold -2.16% 24h."],
            "patterns_detected": [],
            "pattern_details": {},
            "next_events": [],
            "data_snapshot": {"dxy": {"value": 98.47}},
            "macro_trend": {"yields": {"direction": "UP"}},
            "correlations": {"gold_dxy": {"value": -0.19}},
            "source": "mimo",                # MUST STRIP
            "error": None,                   # MUST STRIP
            "headlines_consumed": [{"title": "x"}],  # MUST STRIP
        }
        section = _build_luna_section(raw_luna)
        assert section["available"] is True
        assert "source" not in section
        assert "error" not in section
        assert "headlines_consumed" not in section
        # Kept fields present
        assert section["key_factors"] == ["Gold -2.16% 24h."]
        assert section["data_snapshot"] == {"dxy": {"value": 98.47}}
        assert section["macro_trend"] == {"yields": {"direction": "UP"}}

    def test_echo_section_strips_directional_bias_field(self):
        # Bug G discipline: gold_impact (BULLISH/BEARISH) MUST be stripped.
        # relevance_score (numeric classification-adjacent) MUST be stripped.
        raw_alert = {
            "timestamp": "2026-04-28T16:50:00Z",  # very recent
            "first_seen": "2026-04-28T16:50:00Z",
            "latest": "2026-04-28T16:50:00Z",
            "title": "Gold drops on dollar strength",
            "representative_headline": "Gold drops on dollar strength",
            "headline_count": 1,
            "sources": ["FXStreet"],
            "source": "FXStreet",
            "classification": "IMPORTANT",
            "relevance_score": 85,           # MUST STRIP
            "gold_impact": "BEARISH",        # MUST STRIP
            "summary": "Gold drops...",
            "read": False,
        }
        section = _build_echo_section([raw_alert])
        assert section["available"] is True
        assert section["unread_count"] == 1
        assert section["shown_count"] == 1
        a = section["alerts"][0]
        assert "gold_impact" not in a, (
            "gold_impact directional bias label must NOT appear in "
            "auto-injected echo payload — Bug G discipline."
        )
        assert "relevance_score" not in a, (
            "relevance_score is classification-adjacent — must be stripped."
        )
        assert "read" not in a
        assert "first_seen" not in a
        # Kept fields present
        assert a["classification"] == "IMPORTANT"
        assert a["title"] == "Gold drops on dollar strength"
        assert a["source"] == "FXStreet"
        assert a["summary"] == "Gold drops..."

    def test_echo_filters_read_alerts(self):
        from datetime import datetime, timezone
        recent_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        alerts = [
            {"timestamp": recent_iso, "title": "unread A",
             "classification": "IMPORTANT", "source": "x",
             "summary": "...", "read": False},
            {"timestamp": recent_iso, "title": "read B",
             "classification": "IMPORTANT", "source": "x",
             "summary": "...", "read": True},
            {"timestamp": recent_iso, "title": "unread C",
             "classification": "CRITICAL", "source": "x",
             "summary": "...", "read": False},
        ]
        section = _build_echo_section(alerts)
        assert section["unread_count"] == 2
        titles = [a["title"] for a in section["alerts"]]
        assert "read B" not in titles
        assert "unread A" in titles
        assert "unread C" in titles

    def test_echo_filters_old_alerts(self):
        # Old (>6h) alerts are filtered out per _ECHO_RECENT_HOURS=6
        old = {
            "timestamp": "2026-01-01T00:00:00Z",
            "title": "ancient",
            "classification": "IMPORTANT",
            "source": "x",
            "summary": "...",
            "read": False,
        }
        section = _build_echo_section([old])
        assert section["unread_count"] == 0
        assert section["alerts"] == []

    def test_echo_caps_at_max_alerts(self):
        from datetime import datetime, timezone, timedelta
        # 8 unread + recent alerts; cap should be 5
        now = datetime.now(timezone.utc)
        alerts = []
        for i in range(8):
            ts = (now - timedelta(minutes=i * 10)).isoformat().replace(
                "+00:00", "Z"
            )
            alerts.append({
                "timestamp": ts,
                "title": f"alert_{i}",
                "classification": "IMPORTANT",
                "source": "x",
                "summary": "...",
                "read": False,
            })
        section = _build_echo_section(alerts)
        assert section["unread_count"] == 8
        assert section["shown_count"] == 5
        assert len(section["alerts"]) == 5
        # Newest-first ordering
        assert section["alerts"][0]["title"] == "alert_0"

    def test_block_does_not_mutate_echo_file(self, tmp_path):
        """Non-mutating peek — auto-inject must NOT mark alerts as read."""
        from datetime import datetime, timezone
        recent_iso = datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        echo_path = tmp_path / "echo.json"
        original_alerts = [{
            "timestamp": recent_iso,
            "title": "test",
            "classification": "IMPORTANT",
            "source": "x",
            "summary": "...",
            "read": False,  # explicitly unread
        }]
        echo_path.write_text(json.dumps(original_alerts), encoding="utf-8")
        # Build inject — should not write back
        _ = build_intelligence_block(
            luna_path=str(tmp_path / "missing.json"),
            echo_path=str(echo_path),
        )
        # File contents unchanged: read remains False
        after = json.loads(echo_path.read_text(encoding="utf-8"))
        assert after[0]["read"] is False, (
            "build_intelligence_block must not mark Echo alerts as read"
        )

    def test_age_minutes_returns_none_on_missing_input(self):
        assert _intel_age_minutes(None) is None
        assert _intel_age_minutes("") is None

    def test_age_minutes_handles_z_suffix_and_offset(self):
        # Both ISO formats Luna and Echo use should parse
        a = _intel_age_minutes("2026-04-28T00:00:00Z")
        b = _intel_age_minutes("2026-04-28T00:00:00+00:00")
        assert a is not None and b is not None
        # Same instant -> same age
        assert abs(a - b) < 0.5
