"""Settings → AI over BABA's ai_settings: the model a provider had becomes the
`image` role's (109), the key goes in sealed with BABA's secret, and each way
an image call can be unready is a 400 naming it — the auto-analyzer reads 400
as a standing condition."""

import asyncio
from pathlib import Path

import asyncpg
import pytest
from baba_api.ai import ai_settings, pick_model
from baba_api.crypto import encrypt_secret
from fastapi import HTTPException

MIGRATION = Path(__file__).resolve().parent.parent / "db" / "migrations" / "109_an_ai_model_per_role.sql"
SECRET = "baba-ai-settings-test-secret-0123456789"
KEY = "sk-ant-api03-REAL-SECRET-0123456789-wxyz"


def run(pg, body):
    async def main():
        pool = await asyncpg.create_pool(pg, min_size=1, max_size=2)
        try:
            await body(pool)
        finally:
            await pool.close()
    asyncio.run(main())


def test_the_model_a_provider_had_becomes_the_image_roles(pg):
    async def body(pool):
        await pool.execute("ALTER TABLE ai_settings DROP COLUMN models, ADD COLUMN model text NOT NULL")
        await pool.execute(
            "INSERT INTO ai_settings (provider, model, api_key_encrypted) VALUES ('anthropic', 'claude-opus-5-5', $1)",
            encrypt_secret(KEY, SECRET),
        )
        await pool.execute(MIGRATION.read_text())
        pick = await pick_model(ai_settings(pool, SECRET))
        assert (pick.provider, pick.api_key, pick.model) == ("anthropic", KEY, "claude-opus-5-5")
    run(pg, body)


def test_the_key_is_sealed_and_each_unready_state_is_a_400(pg):
    async def body(pool):
        ai = ai_settings(pool, SECRET)
        with pytest.raises(HTTPException, match="no AI provider is configured") as e:
            await pick_model(ai)
        assert e.value.status_code == 400

        await ai.put("openai", api_key="sk-openai-key-0123456789", enabled=True, models={"image": "gpt-5"})
        await ai.put("anthropic", api_key=KEY, enabled=True, models={"image": "claude-sonnet-5-5"})
        sealed = await pool.fetchval("SELECT api_key_encrypted FROM ai_settings WHERE provider = 'anthropic'")
        assert KEY.encode() not in bytes(sealed)
        assert (await pick_model(ai)).provider == "anthropic", "Anthropic first when both are enabled"
        assert (await pick_model(ai, "openai")).model == "gpt-5"

        await ai.put("anthropic", api_key=None, enabled=False, models={"image": "claude-sonnet-5-5"})
        assert (await pick_model(ai)).provider == "openai", "a disabled provider is passed over"

        with pytest.raises(HTTPException, match="will not decrypt"):
            await pick_model(ai_settings(pool, "a-different-secret-0123456789abcdef"))
    run(pg, body)
