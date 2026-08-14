"""Prompt registry tests — Langfuse store behaviour via a stub client, plus
the in-code fallback path. (The REAL Langfuse round-trip is exercised by
scripts/verify_ai_core.py against the running stack.)"""

from __future__ import annotations

import pytest

from app.ai.prompts import PROMPT_DEFAULTS, PromptRegistry, RegisteredPrompt


class _StubPromptClient:
    """Mimics langfuse.Langfuse prompt management."""

    class _Prompt:
        def __init__(self, text: str, version: int) -> None:
            self.prompt = text
            self.version = version

    def __init__(self, fail: bool = False) -> None:
        self.store: dict[str, list[str]] = {}
        self.fail = fail
        self.get_calls = 0

    def get_prompt(self, name, *, label=None, type="text", max_retries=None, fetch_timeout_seconds=None):
        self.get_calls += 1
        if self.fail:
            raise ConnectionError("langfuse down")
        if name not in self.store:
            raise LookupError(f"prompt {name} not found")
        versions = self.store[name]
        return self._Prompt(versions[-1], len(versions))

    def create_prompt(self, *, name, prompt, labels, type="text"):
        if self.fail:
            raise ConnectionError("langfuse down")
        self.store.setdefault(name, []).append(prompt)


NAME = "careloop_next_best_question"


class TestRegistry:
    def test_missing_prompt_is_published_then_served_with_version(self):
        client = _StubPromptClient()
        registry = PromptRegistry(client=client)
        resolved = registry.get(NAME)
        assert resolved.source == "langfuse"
        assert resolved.version == f"{NAME}@v1"
        assert resolved.text == PROMPT_DEFAULTS[NAME]
        assert client.store[NAME] == [PROMPT_DEFAULTS[NAME]]

    def test_server_version_number_recorded(self):
        client = _StubPromptClient()
        client.store[NAME] = ["old text", "new text"]  # two server versions
        registry = PromptRegistry(client=client)
        resolved = registry.get(NAME)
        assert resolved.version == f"{NAME}@v2"
        assert resolved.text == "new text"

    def test_cache_avoids_refetch_within_ttl(self):
        client = _StubPromptClient()
        registry = PromptRegistry(client=client, cache_ttl_seconds=300)
        registry.get(NAME)
        calls_after_first = client.get_calls
        registry.get(NAME)
        assert client.get_calls == calls_after_first

    def test_langfuse_down_serves_fallback_with_honest_version(self):
        registry = PromptRegistry(client=_StubPromptClient(fail=True))
        resolved = registry.get(NAME)
        assert resolved.source == "fallback"
        assert resolved.version == f"{NAME}@fallback"
        assert resolved.text == PROMPT_DEFAULTS[NAME]

    def test_unknown_prompt_name_raises(self):
        registry = PromptRegistry(client=_StubPromptClient())
        with pytest.raises(KeyError):
            registry.get("no_such_prompt")

    def test_compile_substitutes_variables_and_leaves_unknown(self):
        prompt = RegisteredPrompt(name="p", text="max {{max_suggestions}} and {{other}}",
                                  version="p@v1", source="langfuse")
        assert prompt.compile(max_suggestions=2) == "max 2 and {{other}}"

    def test_every_default_prompt_mentions_synthetic_safety(self):
        for name, text in PROMPT_DEFAULTS.items():
            assert "SYNTHETIC" in text or "synthetic" in text, name
