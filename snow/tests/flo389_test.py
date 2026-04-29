"""FLO-389 — LLM_PROVIDER=gemini switch tests.

Mirrors FLO-384's resolution-matrix discipline. Verifies the
config-load-time resolution of FLOKI_MODEL / FLOKI_API_BASE /
FLOKI_API_KEY when LLM_PROVIDER=gemini, plus cross-leak guards
between provider env namespaces.

Resolution matrix:
  LLM_PROVIDER=gemini  → Google OpenAI-compat base + gemini-3.1-pro-preview model
  LLM_PROVIDER=qwen    → still resolves DashScope (FLO-384 unchanged)
  LLM_PROVIDER=kimi    → still resolves Moonshot (FLO-384 unchanged)
  LLM_PROVIDER=invalid → ValueError naming all three valid providers
"""
from __future__ import annotations

import importlib

import pytest


_PROVIDER_KEYS = (
    "LLM_PROVIDER",
    "FLOKI_MODEL", "FLOKI_API_BASE",
    "QWEN_API_KEY",
    "KIMI_API_KEY", "KIMI_BASE_URL", "KIMI_MODEL",
    "GEMINI_API_KEY", "GEMINI_BASE_URL", "GEMINI_MODEL",
    "FLOKI_FALLBACK_API_BASE", "FLOKI_FALLBACK_API_KEY",
    "FLOKI_FALLBACK_MODEL",
)


def _reload_config(monkeypatch, **env):
    import dotenv
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **kw: False)
    for k in _PROVIDER_KEYS:
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import config as _cfg
    return importlib.reload(_cfg)


# =============================================================================
# Gemini switch — resolution matrix
# =============================================================================


class TestGeminiSwitch:
    def test_explicit_gemini_resolves_googleapis_base(self, monkeypatch):
        cfg = _reload_config(
            monkeypatch, LLM_PROVIDER="gemini", GEMINI_API_KEY="gkey-1",
        )
        assert cfg.LLM_PROVIDER == "gemini"
        assert "googleapis.com" in cfg.FLOKI_API_BASE
        assert cfg.FLOKI_MODEL == "gemini-3.1-pro-preview"
        assert cfg.FLOKI_API_KEY == "gkey-1"

    def test_gemini_minimal_env_uses_defaults(self, monkeypatch):
        """Minimal flip: LLM_PROVIDER=gemini + GEMINI_API_KEY is enough."""
        cfg = _reload_config(
            monkeypatch, LLM_PROVIDER="gemini", GEMINI_API_KEY="gkey-2",
        )
        # Default base must be Google's OpenAI-compat endpoint
        assert cfg.FLOKI_API_BASE == (
            "https://generativelanguage.googleapis.com/v1beta/openai/"
        )
        # Default model must be gemini-3.1-pro-preview (Google's served name)
        assert cfg.FLOKI_MODEL == "gemini-3.1-pro-preview"

    def test_gemini_model_override(self, monkeypatch):
        cfg = _reload_config(
            monkeypatch,
            LLM_PROVIDER="gemini",
            GEMINI_API_KEY="gkey-3",
            GEMINI_MODEL="gemini-3-pro",
        )
        assert cfg.FLOKI_MODEL == "gemini-3-pro"

    def test_gemini_base_url_override(self, monkeypatch):
        cfg = _reload_config(
            monkeypatch,
            LLM_PROVIDER="gemini",
            GEMINI_API_KEY="gkey-4",
            GEMINI_BASE_URL="https://my-proxy.example/v1/",
        )
        assert cfg.FLOKI_API_BASE == "https://my-proxy.example/v1/"

    def test_gemini_does_not_pick_up_qwen_key(self, monkeypatch):
        """Setting QWEN_API_KEY without flipping back to qwen must NOT
        cross-wire credentials to Google's endpoint."""
        cfg = _reload_config(
            monkeypatch,
            LLM_PROVIDER="gemini",
            GEMINI_API_KEY="gkey-5",
            QWEN_API_KEY="qkey-leak",
        )
        assert cfg.LLM_PROVIDER == "gemini"
        assert cfg.FLOKI_API_KEY == "gkey-5"
        assert "googleapis.com" in cfg.FLOKI_API_BASE
        assert "dashscope" not in cfg.FLOKI_API_BASE.lower()

    def test_gemini_does_not_pick_up_kimi_key(self, monkeypatch):
        """Setting KIMI_API_KEY without flipping back to kimi must NOT
        cross-wire Moonshot credentials to Google's endpoint."""
        cfg = _reload_config(
            monkeypatch,
            LLM_PROVIDER="gemini",
            GEMINI_API_KEY="gkey-6",
            KIMI_API_KEY="kkey-leak",
        )
        assert cfg.LLM_PROVIDER == "gemini"
        assert cfg.FLOKI_API_KEY == "gkey-6"
        assert "moonshot" not in cfg.FLOKI_API_BASE.lower()

    def test_gemini_with_no_api_key_resolves_empty_string(self, monkeypatch):
        """No GEMINI_API_KEY → FLOKI_API_KEY is empty (not raise). The
        downstream client init in ai_agent.py decides what to do (typically
        log warning + disable agent). FLO-384 has identical behavior for
        kimi without KIMI_API_KEY."""
        cfg = _reload_config(monkeypatch, LLM_PROVIDER="gemini")
        assert cfg.LLM_PROVIDER == "gemini"
        assert cfg.FLOKI_API_KEY == ""


# =============================================================================
# Cross-provider isolation — flipping to gemini does not break qwen/kimi paths
# =============================================================================


class TestProviderIsolation:
    def test_qwen_default_still_works_after_gemini_branch_added(self, monkeypatch):
        """FLO-384 contract preserved: default (no LLM_PROVIDER) still
        resolves Qwen+DashScope. Smoke check that the new gemini elif
        branch did not regress the default path."""
        cfg = _reload_config(monkeypatch, QWEN_API_KEY="qkey-baseline")
        assert cfg.LLM_PROVIDER == "qwen"
        assert "dashscope" in cfg.FLOKI_API_BASE
        assert cfg.FLOKI_MODEL == "qwen3.6-plus"

    def test_kimi_explicit_still_works(self, monkeypatch):
        """FLO-384 kimi branch unchanged."""
        cfg = _reload_config(
            monkeypatch, LLM_PROVIDER="kimi", KIMI_API_KEY="kkey-baseline",
        )
        assert cfg.LLM_PROVIDER == "kimi"
        assert "moonshot" in cfg.FLOKI_API_BASE.lower()
        assert cfg.FLOKI_MODEL == "kimi-k2.5"


# =============================================================================
# Invalid LLM_PROVIDER fails loudly with three-provider error
# =============================================================================


class TestInvalidProviderFailsLoudly:
    def test_invalid_provider_raises(self, monkeypatch):
        with pytest.raises(ValueError) as exc_info:
            _reload_config(monkeypatch, LLM_PROVIDER="claude")
        msg = str(exc_info.value)
        # Error must name all three valid providers (FLO-389 contract:
        # never silently route to a default — fail with the full menu)
        assert "qwen" in msg.lower()
        assert "kimi" in msg.lower()
        assert "gemini" in msg.lower()
        assert "claude" in msg.lower()  # echoes the bad value back

    def test_invalid_provider_error_references_flo389(self, monkeypatch):
        with pytest.raises(ValueError) as exc_info:
            _reload_config(monkeypatch, LLM_PROVIDER="anthropic")
        msg = str(exc_info.value)
        # FLO-389 ticket reference helps operators trace the gate
        assert "FLO-389" in msg or "FLO-384" in msg


# =============================================================================
# ai_agent.py provider label — googleapis hostname → "Gemini"
# =============================================================================


class TestProviderLabelGemini:
    def test_googleapis_base_url_labels_as_gemini(self):
        """The dynamic provider label in `_initialize_client` (ai_agent.py)
        must classify Google's OpenAI-compat base URL as 'Gemini'.
        Locks the log-line shape so post-restart smoke tests can grep
        `primary client = Gemini` reliably."""
        # Reproduce the inline _provider_label closure shape from
        # ai_agent.py:701-714. We can't easily import it (lives inside
        # AIAgent.initialize()); instead lock the canonical hostname mapping.
        from urllib.parse import urlparse

        def _provider_label(_b: str) -> str:
            _h = (urlparse(_b or "").hostname or "").lower()
            if "moonshot" in _h:
                return "Kimi"
            if "dashscope" in _h:
                return "Qwen"
            if "googleapis.com" in _h:
                return "Gemini"
            if "openrouter.ai" in _h:
                return "OpenRouter"
            if "openai.com" in _h:
                return "OpenAI"
            return _h or "primary"

        # Verify the test reproduction matches the ai_agent.py source
        import inspect
        from ai_agent import AIAgent
        src = inspect.getsource(AIAgent.initialize)
        assert "googleapis.com" in src, (
            "ai_agent.py:_provider_label must classify googleapis.com "
            "hostname as Gemini (FLO-389 contract)"
        )
        assert '"Gemini"' in src, (
            "ai_agent.py:_provider_label must return literal 'Gemini' "
            "string for googleapis.com hostname"
        )

        # Behavioral test on the reproduction
        assert _provider_label(
            "https://generativelanguage.googleapis.com/v1beta/openai/"
        ) == "Gemini"
        # And isolation: other hostnames don't accidentally resolve to Gemini
        assert _provider_label("https://api.moonshot.ai/v1") == "Kimi"
        assert _provider_label(
            "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
        ) == "Qwen"
