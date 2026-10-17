import asyncio

import httpx
import pytest
from baba_api import ai_provider
from baba_api.ai_provider import ProviderModelsError, anthropic_models, openai_chat_models


def _serve(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(
        ai_provider.httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )


def _claude(model_id: str, name: str, *, image: bool) -> dict:
    return {
        "type": "model",
        "id": model_id,
        "display_name": name,
        "created_at": "2026-10-01T00:00:00Z",
        "capabilities": {"image_input": {"supported": image}},
    }


def test_anthropic_list_follows_pages_and_keeps_only_image_models(monkeypatch):
    pages = {
        None: {
            "data": [
                _claude("claude-opus-5-5", "Claude Opus 5.5", image=True),
                _claude("claude-text-1", "Claude Text 1", image=False),
            ],
            "has_more": True,
            "last_id": "claude-text-1",
        },
        "claude-text-1": {
            "data": [_claude("claude-haiku-5-5", "Claude Haiku 5.5", image=True)],
            "has_more": False,
            "last_id": "claude-haiku-5-5",
        },
    }
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "sk-ant-test-key"
        after = request.url.params.get("after_id")
        seen.append(after)
        return httpx.Response(200, json=pages[after])

    _serve(monkeypatch, handler)
    found = asyncio.run(anthropic_models("sk-ant-test-key"))

    assert [m.id for m in found] == ["claude-opus-5-5", "claude-haiku-5-5"]
    assert found[0].label == "Claude Opus 5.5"
    assert seen == [None, "claude-text-1"]


def test_anthropic_list_failure_is_loud(monkeypatch):
    _serve(
        monkeypatch,
        lambda r: httpx.Response(401, json={"error": {"message": "invalid x-api-key"}}),
    )
    with pytest.raises(ProviderModelsError, match="401: invalid x-api-key"):
        asyncio.run(anthropic_models("sk-ant-bad-key"))


def test_anthropic_list_with_unknown_shape_is_loud(monkeypatch):
    _serve(monkeypatch, lambda r: httpx.Response(200, json={"data": [{"id": "claude-x"}]}))
    with pytest.raises(ProviderModelsError, match="unexpected model list"):
        asyncio.run(anthropic_models("sk-ant-test-key"))


def test_openai_list_keeps_chat_aliases_newest_first():
    ids = {
        "gpt-4o": 10,
        "gpt-4o-2024-08-06": 11,
        "gpt-4o-audio-preview": 12,
        "gpt-4o-mini-tts": 13,
        "gpt-4o-transcribe": 14,
        "gpt-realtime": 15,
        "gpt-image-1": 16,
        "gpt-3.5-turbo": 17,
        "gpt-4-0613": 18,
        "gpt-4.1": 20,
        "gpt-5": 30,
        "gpt-5-2025-08-07": 31,
        "gpt-5-pro": 32,
        "gpt-5-codex": 33,
        "o3": 25,
        "o3-deep-research": 26,
        "o4-mini": 27,
        "text-embedding-3-large": 40,
        "whisper-1": 41,
        "dall-e-3": 42,
        "omni-moderation-latest": 43,
        "ft:gpt-4o:acme::abc123": 44,
    }
    data = [{"id": i, "object": "model", "created": c, "owned_by": "system"} for i, c in ids.items()]

    assert [m.id for m in openai_chat_models(data)] == ["gpt-5", "o4-mini", "o3", "gpt-4.1", "gpt-4o"]


def test_claude_answer_cut_at_max_tokens_is_a_failure(monkeypatch):
    body = {
        "model": "claude-opus-5-5",
        "stop_reason": "max_tokens",
        "content": [{"type": "text", "text": '{"zones": [{"name": "Ula'}],
        "usage": {"input_tokens": 900, "output_tokens": 8000},
    }
    _serve(monkeypatch, lambda r: httpx.Response(200, json=body))
    result = asyncio.run(ai_provider.anthropic_text_json("sk-ant-test-key", "claude-opus-5-5", "s", "u"))

    assert not result.ok
    assert result.error == "stop_reason=max_tokens, output_tokens=8000"


def test_claude_refusal_names_its_category(monkeypatch):
    body = {
        "model": "claude-opus-5-5",
        "stop_reason": "refusal",
        "stop_details": {"type": "refusal", "category": "cyber", "explanation": None},
        "content": [],
        "usage": {"input_tokens": 10, "output_tokens": 0},
    }
    _serve(monkeypatch, lambda r: httpx.Response(200, json=body))
    result = asyncio.run(ai_provider.anthropic_ping("sk-ant-test-key", "claude-opus-5-5"))

    assert not result.ok
    assert result.error == "stop_reason=refusal (cyber), output_tokens=0"


def test_gateway_html_is_not_dumped_into_the_ui(monkeypatch):
    _serve(monkeypatch, lambda r: httpx.Response(502, text="<html><body>Bad gateway</body></html>"))
    result = asyncio.run(ai_provider.openai_ping("sk-test-key-123", "gpt-5"))

    assert result.error == "502: upstream gateway error (OpenAI/Cloudflare unavailable)"
