"""FLO-384 — LLM_PROVIDER switch tests.

Verifies the config-load-time resolution of FLOKI_MODEL /
FLOKI_API_BASE / FLOKI_API_KEY from LLM_PROVIDER + provider-specific
env vars. The existing OpenAI client init path consumes the resolved
triple unchanged, so the only contract worth pinning is the
resolution itself.

Resolution matrix:
  LLM_PROVIDER=qwen (default) → DashScope base + qwen3.6-plus model
  LLM_PROVIDER=kimi           → Moonshot base + kimi-k2.5 model
  LLM_PROVIDER=invalid        → ValueError at import time
"""
from __future__ import annotations

import importlib
import os

import pytest


_PROVIDER_KEYS = (
    "LLM_PROVIDER",
    "FLOKI_MODEL", "FLOKI_API_BASE",
    "QWEN_API_KEY",
    "KIMI_API_KEY", "KIMI_BASE_URL", "KIMI_MODEL",
    "FLOKI_FALLBACK_API_BASE", "FLOKI_FALLBACK_API_KEY",
    "FLOKI_FALLBACK_MODEL",
)


def _reload_config(monkeypatch, **env):
    """Set env, reload `config` module, return it.

    Patches `dotenv.load_dotenv` to a no-op so the project's .env file
    cannot override the test's env stub. Without this, `config.py`'s
    `load_dotenv(override=True)` would re-overwrite our setenv with
    the live .env values, and tests would silently exercise the real
    operator config instead of the intended test scenario.
    """
    import dotenv
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **kw: False)
    # Clear any existing provider-related env so test cases don't bleed.
    for k in _PROVIDER_KEYS:
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import config as _cfg
    return importlib.reload(_cfg)


class TestQwenDefault:
    def test_no_llm_provider_set_defaults_to_qwen(self, monkeypatch):
        cfg = _reload_config(monkeypatch, QWEN_API_KEY="qkey-1")
        assert cfg.LLM_PROVIDER == "qwen"
        assert cfg.FLOKI_MODEL == "qwen3.6-plus"
        assert "dashscope" in cfg.FLOKI_API_BASE
        assert cfg.FLOKI_API_KEY == "qkey-1"

    def test_explicit_qwen_resolves_dashscope(self, monkeypatch):
        cfg = _reload_config(
            monkeypatch, LLM_PROVIDER="qwen", QWEN_API_KEY="qkey-2",
        )
        assert cfg.LLM_PROVIDER == "qwen"
        assert "dashscope-intl.aliyuncs.com" in cfg.FLOKI_API_BASE
        assert cfg.FLOKI_API_KEY == "qkey-2"

    def test_qwen_does_not_pick_up_kimi_key(self, monkeypatch):
        """Setting KIMI_API_KEY without flipping LLM_PROVIDER must NOT
        cross-wire Floki to Moonshot. Guards against accidental leakage
        if operator stages KIMI_* env vars before flipping the switch."""
        cfg = _reload_config(
            monkeypatch, QWEN_API_KEY="qkey-3", KIMI_API_KEY="kkey-leak",
        )
        assert cfg.LLM_PROVIDER == "qwen"
        assert cfg.FLOKI_API_KEY == "qkey-3"
        assert "moonshot" not in cfg.FLOKI_API_BASE.lower()


class TestKimiSwitch:
    def test_kimi_resolves_moonshot_base_and_model(self, monkeypatch):
        cfg = _reload_config(
            monkeypatch, LLM_PROVIDER="kimi", KIMI_API_KEY="kkey-1",
        )
        assert cfg.LLM_PROVIDER == "kimi"
        assert cfg.FLOKI_MODEL == "kimi-k2.5"
        assert cfg.FLOKI_API_BASE == "https://api.moonshot.ai/v1"
        assert cfg.FLOKI_API_KEY == "kkey-1"

    def test_kimi_with_explicit_overrides(self, monkeypatch):
        cfg = _reload_config(
            monkeypatch,
            LLM_PROVIDER="kimi",
            KIMI_API_KEY="kkey-2",
            KIMI_MODEL="kimi-k2.6",
            KIMI_BASE_URL="https://api.moonshot.cn/v1",
        )
        assert cfg.FLOKI_MODEL == "kimi-k2.6"
        assert cfg.FLOKI_API_BASE == "https://api.moonshot.cn/v1"
        assert cfg.FLOKI_API_KEY == "kkey-2"

    def test_kimi_missing_key_resolves_empty_not_silent_qwen(self, monkeypatch):
        """If LLM_PROVIDER=kimi but KIMI_API_KEY unset, FLOKI_API_KEY
        must be empty — NOT silently fall through to QWEN_API_KEY.
        Empty key surfaces as init-time disable rather than routing
        to the wrong provider."""
        cfg = _reload_config(
            monkeypatch, LLM_PROVIDER="kimi", QWEN_API_KEY="qkey-decoy",
        )
        assert cfg.LLM_PROVIDER == "kimi"
        assert cfg.FLOKI_API_KEY == ""
        assert "moonshot" in cfg.FLOKI_API_BASE.lower()

    def test_kimi_case_insensitive(self, monkeypatch):
        cfg = _reload_config(
            monkeypatch, LLM_PROVIDER="KIMI", KIMI_API_KEY="kkey-3",
        )
        assert cfg.LLM_PROVIDER == "kimi"
        assert cfg.FLOKI_API_BASE == "https://api.moonshot.ai/v1"


class TestInvalidProvider:
    def test_unknown_provider_raises_loud(self, monkeypatch):
        with pytest.raises(ValueError) as excinfo:
            _reload_config(monkeypatch, LLM_PROVIDER="claude")
        msg = str(excinfo.value)
        assert "claude" in msg.lower() or "not supported" in msg.lower()
        assert "qwen" in msg.lower() and "kimi" in msg.lower()


class TestAgentInitIntegration:
    """Exercise AIAgent.initialize() directly to verify the resolved
    config triple flows through to the OpenAI client construction.
    The unit tests above verify config layer; these verify the
    integration layer where advisor caught a cross-wiring bug
    (ai_agent.py was reading QWEN_API_KEY from env as a fallback,
    bypassing config's provider gate).
    """

    def _make_agent_with_env(self, monkeypatch, **env):
        # Reload config so AIAgent reads the test env triple.
        cfg = _reload_config(
            monkeypatch,
            USE_AI_AGENT="True",  # so init doesn't short-circuit on disabled
            **env,
        )
        # Force USE_AI_AGENT True at the module level (bool, not env str).
        monkeypatch.setattr(cfg, "USE_AI_AGENT", True, raising=False)
        # Reload ai_agent to pick up the reloaded config singleton.
        import ai_agent as _ai
        importlib.reload(_ai)
        return _ai.AIAgent(), cfg

    def test_kimi_provider_with_key_initializes_to_moonshot(self, monkeypatch):
        agent, cfg = self._make_agent_with_env(
            monkeypatch, LLM_PROVIDER="kimi", KIMI_API_KEY="kkey-init",
        )
        ok = agent.initialize()
        assert ok, "agent should initialize with valid Kimi config"
        assert agent.client is not None
        # OpenAI SDK exposes base_url as a URL-like; coerce + verify host.
        assert "moonshot" in str(agent.client.base_url).lower(), (
            f"client base_url should point at Moonshot; got "
            f"{agent.client.base_url}"
        )
        assert agent.model == "kimi-k2.5"

    def test_kimi_missing_key_does_not_cross_wire_qwen_key(
        self, monkeypatch,
    ):
        """Advisor-flagged regression. Operator has QWEN_API_KEY in env
        from the prior Qwen run. They flip LLM_PROVIDER=kimi but forget
        to set KIMI_API_KEY. A naive fallback would send Qwen creds to
        Moonshot. With the FLO-384 fix, the agent disables cleanly."""
        agent, cfg = self._make_agent_with_env(
            monkeypatch,
            LLM_PROVIDER="kimi",
            QWEN_API_KEY="qkey-stale-from-prior-run",
            # KIMI_API_KEY intentionally absent
        )
        # Also clear OPENAI_API_KEY so the secondary "OpenAI fallback"
        # path doesn't mask the test (real env may have it set).
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        ok = agent.initialize()
        assert ok is False, (
            "agent must disable when LLM_PROVIDER=kimi and KIMI_API_KEY "
            "is unset — must NOT fall through to QWEN_API_KEY"
        )
        assert agent.enabled is False

    def test_qwen_provider_uses_qwen_key_via_config(self, monkeypatch):
        agent, cfg = self._make_agent_with_env(
            monkeypatch, LLM_PROVIDER="qwen", QWEN_API_KEY="qkey-init",
        )
        ok = agent.initialize()
        assert ok
        assert "dashscope" in str(agent.client.base_url).lower()
        assert agent.model == "qwen3.6-plus"


class TestFallbackUntouchedByProviderSwitch:
    """Per FLO-384 v1: OpenRouter/Qwen fallback semantics are unchanged
    regardless of LLM_PROVIDER. Documented Option-A behavior — flipping
    to Kimi does NOT auto-rewrite the fallback to Kimi-on-OpenRouter."""

    def test_fallback_model_default_is_qwen(self, monkeypatch):
        cfg = _reload_config(
            monkeypatch, LLM_PROVIDER="kimi", KIMI_API_KEY="kkey-4",
        )
        # Even with primary=Kimi, fallback model default is Qwen.
        assert cfg.FLOKI_FALLBACK_MODEL == "qwen/qwen3.6-plus"

    def test_fallback_base_unset_keeps_disabled(self, monkeypatch):
        cfg = _reload_config(
            monkeypatch, LLM_PROVIDER="kimi", KIMI_API_KEY="kkey-5",
        )
        assert cfg.FLOKI_FALLBACK_API_BASE == ""
        assert cfg.FLOKI_FALLBACK_API_KEY == ""
