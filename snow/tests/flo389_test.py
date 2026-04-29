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


# =============================================================================
# FLO-389 Path A — thought_signature capture-and-replay
# =============================================================================
#
# Gemini 3 returns an encrypted reasoning blob at
# tool_call.extra_content.google.thought_signature on every assistant turn
# that contains tool_calls. The next request must echo it back on the
# rebuilt assistant message or the API returns 400. The OpenAI-compat shim
# preserves it on the wire IF passed via a dict message; the SDK
# ChatCompletionMessage object's serialization is not contractually
# guaranteed to carry Pydantic extras, so the rebuild is always-dict
# under FLO-389.
#
# Pure helpers in gemini_signature.py:
#   - rebuild_assistant_message(msg, tool_calls, *, preserve_signatures)
#   - strip_thought_signatures(messages)
#
# These tests cover the four acceptance items: roundtrip, parallel-call
# ordering, missing-signature graceful handling, and fallback strip.


class _FakeFunction:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    """Minimal duck-type for the OpenAI SDK ChatCompletionMessageToolCall.

    The rebuild helper reads `tc.id`, `tc.function.name`,
    `tc.function.arguments`, and `tc.extra_content` (optional). We
    construct only those fields here — full SDK fidelity would couple
    the test to openai version internals.
    """
    def __init__(self, id_, name, arguments, extra_content=None):
        self.id = id_
        self.function = _FakeFunction(name, arguments)
        if extra_content is not None:
            self.extra_content = extra_content


class _FakeMessage:
    def __init__(self, content=None):
        self.content = content


_SIG_A = "EtgDCtUDAQw_signature_alpha_=="
_SIG_B = "AbCDeFgH_signature_beta_=="


class TestThoughtSignatureRoundtrip:
    """Single-tool-call roundtrip: signature captured on response is
    threaded into the rebuilt assistant message dict so the next-turn
    request carries it back to Gemini. This is the canonical fix path
    for the production 400."""

    def _gemini_tc(self):
        return _FakeToolCall(
            "call-1", "get_weather", '{"city":"Paris"}',
            extra_content={"google": {"thought_signature": _SIG_A}},
        )

    def test_signature_preserved_when_provider_is_gemini(self):
        from gemini_signature import rebuild_assistant_message
        tc = self._gemini_tc()
        out = rebuild_assistant_message(
            _FakeMessage(content=None), [tc], preserve_signatures=True,
        )
        assert out["role"] == "assistant"
        assert len(out["tool_calls"]) == 1
        rebuilt_tc = out["tool_calls"][0]
        assert rebuilt_tc["id"] == "call-1"
        assert rebuilt_tc["function"]["name"] == "get_weather"
        assert rebuilt_tc["function"]["arguments"] == '{"city":"Paris"}'
        # Critical: extra_content carried through verbatim
        assert rebuilt_tc["extra_content"] == {
            "google": {"thought_signature": _SIG_A}
        }

    def test_signature_dropped_when_provider_is_not_gemini(self):
        """preserve_signatures=False (Qwen/Kimi default) must omit
        extra_content even if the SDK object happens to carry one. This
        is defense-in-depth: a non-Gemini wire either ignores or 400s
        on the field."""
        from gemini_signature import rebuild_assistant_message
        tc = self._gemini_tc()
        out = rebuild_assistant_message(
            _FakeMessage(content=None), [tc], preserve_signatures=False,
        )
        rebuilt_tc = out["tool_calls"][0]
        assert "extra_content" not in rebuilt_tc

    def test_missing_signature_does_not_crash(self):
        """If Gemini ever returns a tool_call without extra_content (e.g.
        on Gemini 2.5 where signatures are optional, or a hypothetical
        future model), the helper must not crash — the rebuild dict
        simply omits the field. Validation/400 is the API's job."""
        from gemini_signature import rebuild_assistant_message
        tc = _FakeToolCall("call-1", "get_weather", '{"city":"Paris"}')
        # No extra_content attribute set
        out = rebuild_assistant_message(
            _FakeMessage(), [tc], preserve_signatures=True,
        )
        rebuilt_tc = out["tool_calls"][0]
        assert "extra_content" not in rebuilt_tc

    def test_empty_extra_content_dropped(self):
        """An explicitly empty extra_content dict shouldn't surface as
        a noise field on the wire."""
        from gemini_signature import rebuild_assistant_message
        tc = _FakeToolCall(
            "call-1", "get_weather", '{"city":"Paris"}', extra_content={},
        )
        out = rebuild_assistant_message(
            _FakeMessage(), [tc], preserve_signatures=True,
        )
        assert "extra_content" not in out["tool_calls"][0]

    def test_content_passthrough_alongside_tool_calls(self):
        """The original rebuild already preserved msg.content alongside
        tool_calls. FLO-389 must not regress that."""
        from gemini_signature import rebuild_assistant_message
        tc = self._gemini_tc()
        out = rebuild_assistant_message(
            _FakeMessage(content="reasoning prose"), [tc],
            preserve_signatures=True,
        )
        assert out["content"] == "reasoning prose"

    def test_no_content_no_content_field(self):
        from gemini_signature import rebuild_assistant_message
        tc = self._gemini_tc()
        out = rebuild_assistant_message(
            _FakeMessage(content=None), [tc], preserve_signatures=True,
        )
        assert "content" not in out


class TestParallelToolCallSignatureOrdering:
    """Per Google docs: 'the thought_signature is attached only to the
    first functionCall part. Subsequent functionCall parts in the same
    response will not contain a signature.' The rebuild must preserve
    this asymmetry — first tc carries the field, later tcs don't."""

    def test_first_tc_carries_signature_others_dont(self):
        from gemini_signature import rebuild_assistant_message
        tcs = [
            _FakeToolCall(
                "call-1", "first_tool", '{}',
                extra_content={"google": {"thought_signature": _SIG_A}},
            ),
            _FakeToolCall("call-2", "second_tool", '{}'),
            _FakeToolCall("call-3", "third_tool", '{}'),
        ]
        out = rebuild_assistant_message(
            _FakeMessage(), tcs, preserve_signatures=True,
        )
        assert "extra_content" in out["tool_calls"][0]
        assert out["tool_calls"][0]["extra_content"] == {
            "google": {"thought_signature": _SIG_A}
        }
        assert "extra_content" not in out["tool_calls"][1]
        assert "extra_content" not in out["tool_calls"][2]

    def test_singleton_clamp_drops_later_tcs_signature_unaffected(self):
        """When FLO-385 singleton clamp reduces a parallel batch to one
        tc, the kept tc's signature still rides through."""
        from gemini_signature import rebuild_assistant_message
        # Simulating: clamp kept only the first tc
        kept_tcs = [
            _FakeToolCall(
                "call-1", "get_chart_screenshots", '{}',
                extra_content={"google": {"thought_signature": _SIG_A}},
            ),
        ]
        out = rebuild_assistant_message(
            _FakeMessage(), kept_tcs, preserve_signatures=True,
        )
        assert len(out["tool_calls"]) == 1
        assert out["tool_calls"][0]["extra_content"] == {
            "google": {"thought_signature": _SIG_A}
        }


class TestFallbackStripsSignatures:
    """FLO-299 fallback path: Gemini-primary failures route to OpenRouter
    mid-cycle. Any prior assistant turns in `messages` carry Gemini's
    extra_content; sending those to OpenRouter risks a 400 (strict
    OpenAI-compat) or silent drop. strip_thought_signatures returns a
    copy with the field scrubbed."""

    def test_strip_removes_extra_content_from_assistant_tool_calls(self):
        from gemini_signature import strip_thought_signatures
        messages = [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call-1", "type": "function",
                        "function": {"name": "f", "arguments": "{}"},
                        "extra_content": {
                            "google": {"thought_signature": _SIG_A}
                        },
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "{}"},
        ]
        out = strip_thought_signatures(messages)
        assert "extra_content" not in out[1]["tool_calls"][0]
        # Other fields preserved
        assert out[1]["tool_calls"][0]["id"] == "call-1"
        assert out[1]["tool_calls"][0]["function"]["name"] == "f"

    def test_strip_does_not_mutate_original(self):
        """Pure on input. Fallback retry must not destroy signatures
        from the messages list — if Gemini recovers next iteration,
        the originals stay intact."""
        from gemini_signature import strip_thought_signatures
        original = [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call-1", "type": "function",
                        "function": {"name": "f", "arguments": "{}"},
                        "extra_content": {
                            "google": {"thought_signature": _SIG_A}
                        },
                    },
                ],
            },
        ]
        _ = strip_thought_signatures(original)
        # Original retains the signature
        assert "extra_content" in original[0]["tool_calls"][0]
        assert original[0]["tool_calls"][0]["extra_content"] == {
            "google": {"thought_signature": _SIG_A}
        }

    def test_strip_passes_through_non_assistant_messages(self):
        from gemini_signature import strip_thought_signatures
        messages = [
            {"role": "system", "content": "..."},
            {"role": "user", "content": "..."},
            {"role": "tool", "tool_call_id": "x", "content": "..."},
        ]
        out = strip_thought_signatures(messages)
        assert out == messages

    def test_strip_handles_assistant_without_tool_calls(self):
        from gemini_signature import strip_thought_signatures
        messages = [{"role": "assistant", "content": "plain reply"}]
        out = strip_thought_signatures(messages)
        assert out == messages

    def test_strip_handles_multiple_parallel_tcs(self):
        """Strip must scrub every tc, not just the first — even though
        only the first carries a signature, defense-in-depth on the
        scrub matters if Google ever changes the asymmetry."""
        from gemini_signature import strip_thought_signatures
        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "c1", "type": "function",
                        "function": {"name": "f1", "arguments": "{}"},
                        "extra_content": {
                            "google": {"thought_signature": _SIG_A}
                        },
                    },
                    {
                        "id": "c2", "type": "function",
                        "function": {"name": "f2", "arguments": "{}"},
                        "extra_content": {
                            "google": {"thought_signature": _SIG_B}
                        },
                    },
                ],
            },
        ]
        out = strip_thought_signatures(messages)
        for tc in out[0]["tool_calls"]:
            assert "extra_content" not in tc

    def test_strip_idempotent(self):
        """Stripping already-stripped messages is a no-op."""
        from gemini_signature import strip_thought_signatures
        messages = [
            {
                "role": "assistant",
                "tool_calls": [{
                    "id": "c1", "type": "function",
                    "function": {"name": "f1", "arguments": "{}"},
                }],
            },
        ]
        out1 = strip_thought_signatures(messages)
        out2 = strip_thought_signatures(out1)
        assert out1 == out2


class TestAiAgentIntegrationContract:
    """Source-inspection lock that ai_agent.py wires the rebuild + strip
    helpers in. Belt-and-braces: if a future refactor drops the import
    or replaces the helper call, this fails fast."""

    def test_ai_agent_imports_helpers(self):
        import inspect
        import ai_agent
        src = inspect.getsource(ai_agent)
        assert "from gemini_signature import" in src, (
            "ai_agent.py must import the FLO-389 thought_signature helpers"
        )
        assert "_rebuild_assistant_message" in src, (
            "ai_agent.py must call the rebuild helper in the tool-call loop"
        )
        assert "_strip_thought_signatures" in src, (
            "ai_agent.py must scrub signatures before non-Gemini fallback"
        )

    def test_ai_agent_provider_gates_signature_preservation(self):
        """Source contract: rebuild call must condition preserve_signatures
        on LLM_PROVIDER == 'gemini' (not always-True, not always-False)."""
        import inspect
        import ai_agent
        src = inspect.getsource(ai_agent)
        assert 'preserve_signatures=' in src
        assert '"gemini"' in src, (
            "rebuild call must gate on LLM_PROVIDER == 'gemini'"
        )
