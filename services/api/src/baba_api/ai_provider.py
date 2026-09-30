"""Thin clients around the Anthropic Messages API and OpenAI Chat Completions API.

We deliberately do NOT depend on the `anthropic` or `openai` SDKs to keep our
dep tree narrow — both providers expose a single POST endpoint and we already
have httpx. Both return a normalized `AiCallResult` so the caller (routes_ai)
doesn't need to know which one ran.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)


ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"


@dataclass(slots=True)
class AiCallResult:
    ok: bool
    text: str | None
    model: str | None
    latency_ms: int
    error: str | None
    # Provider-reported usage; None when the call failed before a response.
    input_tokens: int | None = None
    output_tokens: int | None = None


async def anthropic_ping(api_key: str, model: str) -> AiCallResult:
    """Minimal liveness check: 1-token response. Used by the "Test" button."""
    payload = {
        "model": model,
        "max_tokens": 8,
        "messages": [{"role": "user", "content": "Reply with the single word OK."}],
    }
    return await _anthropic_call(api_key, payload)


async def anthropic_text_json(
    api_key: str,
    model: str,
    system: str,
    user_text: str,
    *,
    max_tokens: int = 2000,
) -> AiCallResult:
    """Text-only sibling of anthropic_vision_json. For tasks where the
    model just reasons over structured numbers (e.g. zone-rule tuning
    from per-zone stats), skipping the image saves bandwidth and stops
    the model from being distracted by visual content."""
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": user_text}]},
        ],
    }
    return await _anthropic_call(api_key, payload, http_timeout_s=60)


async def anthropic_vision_json(
    api_key: str,
    model: str,
    system: str,
    user_text: str,
    image_jpeg: bytes,
    *,
    max_tokens: int = 2000,
) -> AiCallResult:
    """Send a single image + text to Claude and return raw response text.

    The caller is responsible for instructing the model to emit JSON in the
    body (via `system`/`user_text`) and for parsing the result.
    """
    b64 = base64.standard_b64encode(image_jpeg).decode("ascii")
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": b64,
                        },
                    },
                    {"type": "text", "text": user_text},
                ],
            }
        ],
    }
    return await _anthropic_call(api_key, payload, http_timeout_s=90)


async def _anthropic_call(
    api_key: str,
    payload: dict,
    *,
    http_timeout_s: float = 30,
) -> AiCallResult:
    start = time.monotonic()
    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=http_timeout_s) as client:
            r = await client.post(ANTHROPIC_URL, headers=headers, json=payload)
    except httpx.HTTPError as e:
        return AiCallResult(
            ok=False,
            text=None,
            model=None,
            latency_ms=int((time.monotonic() - start) * 1000),
            error=f"network: {e.__class__.__name__}: {e}"[:400],
        )

    latency_ms = int((time.monotonic() - start) * 1000)
    if r.status_code != 200:
        # Anthropic returns {"type":"error","error":{"type":"...","message":"..."}}
        # but upstream errors (Cloudflare 502, nginx 504) come back as raw
        # HTML; dumping that into the UI shows hundreds of unstyled tags
        # to the operator. Detect the JSON-vs-HTML case and produce a
        # clean message either way.
        try:
            body = r.json()
            err = body.get("error", {})
            msg = err.get("message") or json.dumps(err)[:300]
        except (ValueError, AttributeError):
            text = (r.text or "").lstrip()
            if text.startswith("<") or "html" in text[:80].lower():
                msg = "upstream gateway error (Anthropic/Cloudflare unavailable)"
            else:
                msg = text[:300]
        return AiCallResult(
            ok=False,
            text=None,
            model=None,
            latency_ms=latency_ms,
            error=f"{r.status_code}: {msg}",
        )

    try:
        body = r.json()
    except ValueError:
        return AiCallResult(
            ok=False,
            text=None,
            model=None,
            latency_ms=latency_ms,
            error="non-JSON response from provider",
        )

    # Claude returns content as a list of blocks; we only need the text blocks.
    blocks = body.get("content") or []
    text_parts = [
        b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"
    ]
    text = "".join(text_parts).strip()
    usage = body.get("usage") or {}
    return AiCallResult(
        ok=True,
        text=text,
        model=body.get("model"),
        latency_ms=latency_ms,
        error=None,
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
    )


# --- OpenAI ---------------------------------------------------------------


async def openai_ping(api_key: str, model: str) -> AiCallResult:
    payload = {
        "model": model,
        # GPT-5 + o-series rejected the old `max_tokens` name on Chat
        # Completions; `max_completion_tokens` is the supported field on
        # all current models, so we use it unconditionally. The budget must
        # cover INTERNAL reasoning tokens too — with 8, a reasoning model
        # (GPT-5, o-series) burns the whole budget before emitting any
        # visible text and the ping fails with finish_reason=length even
        # though the key and model are valid.
        "max_completion_tokens": 1024,
        "messages": [{"role": "user", "content": "Reply with the single word OK."}],
    }
    return await _openai_call(api_key, payload)


async def openai_text_json(
    api_key: str,
    model: str,
    system: str,
    user_text: str,
    *,
    max_tokens: int = 8000,
) -> AiCallResult:
    """Text-only sibling of openai_vision_json. Same `json_object` mode
    so the response comes back as a parseable JSON dict."""
    payload = {
        "model": model,
        "max_completion_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
        ],
    }
    return await _openai_call(api_key, payload, http_timeout_s=60)


async def openai_vision_json(
    api_key: str,
    model: str,
    system: str,
    user_text: str,
    image_jpeg: bytes,
    *,
    max_tokens: int = 8000,
) -> AiCallResult:
    """Vision + JSON-mode call. `response_format=json_object` forces the model
    to emit a single JSON object so we don't need fence-stripping for OpenAI
    (we still tolerate it on the parse side for resilience).

    The default `max_tokens=8000` accommodates reasoning models (GPT-5,
    o-series) which consume tokens internally before producing the visible
    JSON output. With the older 2000 default these models would burn
    through the budget on reasoning and return empty content.
    """
    b64 = base64.standard_b64encode(image_jpeg).decode("ascii")
    payload = {
        "model": model,
        "max_completion_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                    },
                    {"type": "text", "text": user_text},
                ],
            },
        ],
    }
    return await _openai_call(api_key, payload, http_timeout_s=180)


async def _openai_call(
    api_key: str,
    payload: dict,
    *,
    http_timeout_s: float = 30,
) -> AiCallResult:
    start = time.monotonic()
    headers = {
        "authorization": f"Bearer {api_key}",
        "content-type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=http_timeout_s) as client:
            r = await client.post(OPENAI_URL, headers=headers, json=payload)
    except httpx.HTTPError as e:
        return AiCallResult(
            ok=False,
            text=None,
            model=None,
            latency_ms=int((time.monotonic() - start) * 1000),
            error=f"network: {e.__class__.__name__}: {e}"[:400],
        )

    latency_ms = int((time.monotonic() - start) * 1000)
    if r.status_code != 200:
        # OpenAI returns {"error": {"message": "...", "type": "...", ...}}
        # — but Cloudflare/upstream gateways return raw HTML, which is
        # noise in the UI. Same handling as the Anthropic path.
        try:
            body = r.json()
            err = body.get("error", {})
            msg = err.get("message") or json.dumps(err)[:300]
        except (ValueError, AttributeError):
            text = (r.text or "").lstrip()
            if text.startswith("<") or "html" in text[:80].lower():
                msg = "upstream gateway error (OpenAI/Cloudflare unavailable)"
            else:
                msg = text[:300]
        return AiCallResult(
            ok=False,
            text=None,
            model=None,
            latency_ms=latency_ms,
            error=f"{r.status_code}: {msg}",
        )

    try:
        body = r.json()
    except ValueError:
        return AiCallResult(
            ok=False,
            text=None,
            model=None,
            latency_ms=latency_ms,
            error="non-JSON response from provider",
        )

    choices = body.get("choices") or []
    text = ""
    finish_reason = None
    if choices and isinstance(choices[0], dict):
        finish_reason = choices[0].get("finish_reason")
        msg = choices[0].get("message") or {}
        content = msg.get("content")
        if isinstance(content, str):
            text = content.strip()
        elif isinstance(content, list):
            # Some endpoints return content as list of parts; concat text parts.
            text = "".join(
                p.get("text", "")
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            ).strip()

    # Reasoning models (GPT-5, o-series) sometimes burn the whole token
    # budget on internal reasoning and return empty content with
    # finish_reason="length". Surface that as an explicit error so the
    # caller can show a useful message instead of silently treating it
    # as "model gave no result".
    usage = body.get("usage") or {}
    if not text:
        if finish_reason == "length":
            return AiCallResult(
                ok=False,
                text="",
                model=body.get("model"),
                latency_ms=latency_ms,
                error=(
                    f"empty content (finish_reason=length, "
                    f"reasoning_tokens={usage.get('completion_tokens_details', {}).get('reasoning_tokens')}, "
                    f"completion_tokens={usage.get('completion_tokens')}). "
                    f"Raise max_completion_tokens — reasoning model exhausted budget."
                ),
            )
        log.info(
            "openai empty content: finish_reason=%s usage=%s",
            finish_reason,
            usage,
        )

    return AiCallResult(
        ok=True,
        text=text,
        model=body.get("model"),
        latency_ms=latency_ms,
        error=None,
        input_tokens=usage.get("prompt_tokens"),
        output_tokens=usage.get("completion_tokens"),
    )
