"""BABA's language model: one role, `image` — a question about a camera frame,
a zone or a face, answered by a model that reads pictures — with the key sealed
by BABA's own secret, over home_core.llm."""

from __future__ import annotations

import logging

import asyncpg
from fastapi import HTTPException, Request
from home_core.llm import AiNotConfigured, AiSettings, Pick, Role

from baba_api.crypto import decrypt_secret, encrypt_secret

log = logging.getLogger(__name__)

# A role falls back through them in this order.
PROVIDERS = ("anthropic", "openai")
ROLES = (Role("image", frozenset({"image_input"})),)


def ai_settings(pool: asyncpg.Pool, secret_key: str) -> AiSettings:
    def seal(key: str) -> bytes:
        return encrypt_secret(key, secret_key)

    def open_(sealed: bytes) -> str | None:
        try:
            return decrypt_secret(sealed, secret_key)
        except ValueError:
            log.error("the stored AI key will not decrypt (BABA_SECRET_KEY changed or value corrupt)")
            return None

    return AiSettings(pool, providers=PROVIDERS, roles=ROLES, encrypt=seal, decrypt=open_)


def settings_of(request: Request) -> AiSettings:
    return ai_settings(request.app.state.pool, request.app.state.secret_key)


async def pick_model(ai: AiSettings, requested: str | None = None) -> Pick:
    """The provider, key and model an image call goes to. Nothing usable is a
    400 with the reason, which the auto-analyzer reads as a standing condition."""
    try:
        return await ai.pick("image", requested)
    except AiNotConfigured as e:
        raise HTTPException(400, str(e)) from e
