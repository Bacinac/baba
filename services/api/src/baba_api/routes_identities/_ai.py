"""AI-generated identity descriptions."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from uuid import UUID

from fastapi import Depends, HTTPException, Query, Request
from home_core.tasks import spawn

from baba_api.auth import AuthUser, current_user
from baba_api.routes_ai import _pick_active_provider, _vision_json
from baba_api.routes_identities._base import (
    identities_router,
)

log = logging.getLogger(__name__)

_DESCRIBE_SYSTEM_HR = (
    "You are a vision assistant for a home/business surveillance system. "
    "You receive a thumbnail crop of a single tracked object (person, "
    "vehicle, pet, package, robot mower, etc.) and must produce a short "
    "labeling suggestion the operator will confirm.\n\n"
    "Output ONLY a JSON object — no prose, no markdown fences. Fields:\n"
    "  name: <2-6 word descriptive name, in Croatian if the operator "
    'uses Croatian, otherwise English; e.g. "Crna mačka s bijelim šapama">\n'
    '  kind: one of "person", "vehicle", "pet", "animal", "package", "robot", "unknown"\n'
    '  species: for kind="pet"/"animal", the animal itself as one '
    'lowercase English word ("cat", "dog", "bird", ...); null otherwise. '
    "Put it HERE, never in tags — a detector that flips cat<->dog on the "
    "same animal is exactly why the identity has to carry this.\n"
    "  tags: array of 2-6 short descriptive English tags (lowercase, "
    "hyphen-separated for multi-word tags) — colour, vehicle make, size, "
    "distinguishing features. Tags are free description ONLY: never put the "
    "species there, and never anything about who someone IS to the household "
    "(family/neighbour/…) — you cannot know that from an image and a separate "
    "operator-set field owns it.\n"
    "  description: one sentence about distinguishing features the "
    "operator could use to recognise this subject\n"
    '  plate: license plate string when kind="vehicle" AND the plate is '
    "fully readable, formatted as the plate itself prints it "
    '("ZG-1234-AB", "B 4321 EF"); use null otherwise. Never invent or '
    "partially-guess plates — null is better than a wrong plate.\n"
    '  confidence: "low" | "medium" | "high"\n\n'
    "Examples:\n"
    '{"name":"Crna mačka s bijelim šapama","kind":"pet","species":"cat",'
    '"tags":["black","white-paws","small"],'
    '"description":"A mostly-black cat with white front paws and a white chest patch.",'
    '"plate":null,"confidence":"high"}\n'
    '{"name":"Tamno-plava limuzina","kind":"vehicle","species":null,'
    '"tags":["sedan","dark-blue","four-door"],'
    '"description":"A dark blue four-door sedan, mid-size, no visible roof rack.",'
    '"plate":"ZG-1234-AB","confidence":"medium"}\n'
    '{"name":"Robotska kosilica","kind":"robot","species":null,'
    '"tags":["mower","robotic","green","small"],'
    '"description":"A small robotic lawn mower with a green chassis, driving on grass.",'
    '"plate":null,"confidence":"medium"}\n'
)

_DESCRIBE_USER = (
    "Describe this tracked subject. The operator will use your "
    "suggestion to label this identity in the system. Be concise and "
    "concrete — what visible features would let me recognise this "
    "specific subject again later?"
)


def _parse_describe_response(raw: str) -> dict[str, Any] | None:
    """Pluck the JSON object out of the VLM response. Most providers
    return a clean object but the prompt is gentle enough that some wrap
    it in ```json``` fences or add a preamble — strip that defensively."""
    if not raw:
        return None
    text = raw.strip()
    # Strip markdown fences if the model decided to be helpful.
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1 :]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    # Find the first '{' .. last '}' window. Cheap and robust against
    # leading "Here's the JSON:" prefixes.
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        obj = json.loads(text[start : end + 1])
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    return obj


@identities_router.post("/identities/{global_id}/ai-describe")
async def ai_describe_identity(
    global_id: UUID,
    request: Request,
    provider: str | None = Query(default=None),
    _user: AuthUser = Depends(current_user),
) -> dict[str, Any]:
    """Ask the configured AI provider to suggest a name + kind + tags +
    notes for this identity, given its most recent thumbnail. Returns a
    raw suggestion the UI can apply to the label form; nothing is
    written to the DB until the user clicks Save."""
    pool = request.app.state.pool
    # Prefer the focused subject crop (the same pixels the embedder saw) over
    # the wider-context thumbnail — the VLM is much more accurate when it
    # only sees the subject, not the whole scene around it.
    image_rel: str | None = await pool.fetchval(
        """
        SELECT COALESCE(crop_path, thumbnail_path) FROM tracks
        WHERE global_id = $1
          AND (crop_path IS NOT NULL OR thumbnail_path IS NOT NULL)
        ORDER BY ended_at DESC LIMIT 1
        """,
        global_id,
    )
    if image_rel is None:
        raise HTTPException(400, "no image available for this identity")

    media_path = request.app.state.config.media_path
    abs_path = media_path / image_rel
    try:
        image_jpeg = abs_path.read_bytes()
    except FileNotFoundError:
        raise HTTPException(404, f"image file missing: {image_rel}") from None
    except Exception as e:
        raise HTTPException(500, f"failed to read image: {e}") from e

    active_provider, api_key, model = await _pick_active_provider(request, provider)
    result = await _vision_json(
        active_provider,
        api_key,
        model,
        system=_DESCRIBE_SYSTEM_HR,
        user_text=_DESCRIBE_USER,
        image_jpeg=image_jpeg,
    )
    # Stamp last_used_at on the provider row without blocking the response.
    spawn(
        pool.execute(
            "UPDATE ai_settings SET last_used_at = now() WHERE provider = $1",
            active_provider,
        )
    )
    if not result.ok:
        raise HTTPException(502, result.error or "AI call failed")

    suggestion = _parse_describe_response(result.text or "")
    if suggestion is None:
        # Let the UI show the raw response so the user can paste / debug.
        return {
            "ok": False,
            "raw": result.text,
            "model": active_provider + ":" + model,
            "latency_ms": result.latency_ms,
            "error": "could not parse JSON from AI response",
        }
    # Audit row so the per-identity history surfaces "AI suggested X at Y"
    # alongside merges/splits. Truncate description for compact payload.
    await pool.execute(
        "INSERT INTO identity_audit (user_id, op, payload) VALUES ($1, 'ai_describe', $2::jsonb)",
        _user.id,
        json.dumps(
            {
                "gid": str(global_id),
                "model": active_provider + ":" + model,
                "name": suggestion.get("name"),
                "kind": suggestion.get("kind"),
                "plate": suggestion.get("plate"),
                "confidence": suggestion.get("confidence"),
            }
        ),
    )
    return {
        "ok": True,
        "suggestion": suggestion,
        "model": active_provider + ":" + model,
        "latency_ms": result.latency_ms,
        "image_path": image_rel,
    }


# --- auto-describe background loop ---------------------------------------


async def _auto_describe_one(app, pool, gid: UUID, thumb_path: str) -> None:
    """Run AI describe on a single identity and persist the result as a
    new label row. Designed to be invoked from the periodic loop, so it
    never raises — every failure is logged + moves on to the next."""
    media_path = app.state.config.media_path
    abs_path = media_path / thumb_path
    try:
        image_jpeg = abs_path.read_bytes()
    except Exception:
        log.exception("auto-describe %s: failed to read thumbnail", gid)
        return

    # No `request` object here — duplicate the small slice of
    # _pick_active_provider we need, off the app state directly.
    from baba_api.crypto import decrypt_secret
    from baba_api.models import SUPPORTED_AI_PROVIDERS

    secret = app.state.secret_key
    provider_row = None
    for p in SUPPORTED_AI_PROVIDERS:
        r = await pool.fetchrow(
            "SELECT provider, model, api_key_encrypted, enabled FROM ai_settings WHERE provider = $1",
            p,
        )
        if r is not None and r["enabled"]:
            provider_row = r
            break
    if provider_row is None:
        return  # no provider configured — silent skip until operator sets one
    api_key = decrypt_secret(bytes(provider_row["api_key_encrypted"]), secret)
    provider, model = provider_row["provider"], provider_row["model"]

    result = await _vision_json(
        provider,
        api_key,
        model,
        system=_DESCRIBE_SYSTEM_HR,
        user_text=_DESCRIBE_USER,
        image_jpeg=image_jpeg,
    )
    if not result.ok:
        log.warning("auto-describe %s: provider %s failed: %s", gid, provider, result.error)
        return
    suggestion = _parse_describe_response(result.text or "")
    if suggestion is None:
        log.warning("auto-describe %s: unparseable response: %.200s", gid, result.text or "")
        return

    name = (suggestion.get("name") or "").strip() or f"Identitet {str(gid)[:8]}"
    kind = (suggestion.get("kind") or "").strip() or None
    tags_raw = suggestion.get("tags") or []
    tags = [str(t).strip() for t in tags_raw if str(t).strip()][:16]
    species_raw = suggestion.get("species")
    species = str(species_raw).strip().lower()[:32] if species_raw else None
    description = (suggestion.get("description") or "").strip() or None
    plate = suggestion.get("plate") or None
    if isinstance(plate, str):
        plate = plate.strip() or None

    async with pool.acquire() as conn:  # noqa: SIM117 (kept nested on purpose)
        async with conn.transaction():
            # Race guard: somebody (user via UI) may have labelled this
            # identity between the SELECT and this INSERT. ON CONFLICT
            # DO NOTHING means we cleanly yield to that manual label.
            await conn.execute(
                """
                INSERT INTO identity_labels
                    (global_id, name, kind, tags, notes, plate, species, source, ai_described_at)
                VALUES ($1, $2, $3, $4::text[], $5, $6, $7, 'ai', now())
                ON CONFLICT (global_id) DO NOTHING
                """,
                gid,
                name,
                kind,
                tags,
                description,
                plate,
                species,
            )
            if plate:
                # An enrolled plate reaches the reads that predate it — same
                # rule as the manual label route, or the AI-described car's
                # past visits and current episode stay unmatched.
                from baba_api.routes_identities._core import _rematch_plate_reads

                await _rematch_plate_reads(conn)
            await conn.execute(
                "INSERT INTO identity_audit (user_id, op, payload) VALUES (NULL, 'ai_describe', $1::jsonb)",
                json.dumps(
                    {
                        "gid": str(gid),
                        "model": provider + ":" + model,
                        "name": name,
                        "kind": kind,
                        "plate": plate,
                        "confidence": suggestion.get("confidence"),
                        "auto": True,
                    }
                ),
            )
    log.info(
        "auto-describe %s: name=%r kind=%s plate=%s conf=%s",
        gid,
        name,
        kind,
        plate,
        suggestion.get("confidence"),
    )


async def auto_describe_loop(app) -> None:
    """Periodically scan for unlabelled identities and seed them with an
    AI-generated draft. Identities are picked oldest-thumbnail-first so
    backlog drains in chronological order. `auto_describe_batch` caps
    per-tick spending; the loop sleeps `auto_describe_interval_s` between
    ticks, plus a short delay between identities inside a tick so we
    don't burst the AI provider's rate limit."""
    config = app.state.config
    if not config.auto_describe_enabled:
        log.info("auto-describe disabled (BABA_AI_AUTO_DESCRIBE not set)")
        return
    log.info(
        "auto-describe loop: interval=%ds batch=%d",
        config.auto_describe_interval_s,
        config.auto_describe_batch,
    )
    # Initial pause so api startup doesn't immediately fire AI calls.
    await asyncio.sleep(min(30, config.auto_describe_interval_s))
    while True:
        try:
            pool = app.state.pool
            rows = await pool.fetch(
                """
                SELECT DISTINCT ON (t.global_id)
                       t.global_id,
                       COALESCE(t.crop_path, t.thumbnail_path) AS image_path
                FROM tracks t
                LEFT JOIN identity_labels l ON l.global_id = t.global_id
                WHERE t.global_id IS NOT NULL
                  AND (t.crop_path IS NOT NULL OR t.thumbnail_path IS NOT NULL)
                  AND l.global_id IS NULL
                ORDER BY t.global_id, t.ended_at DESC
                LIMIT $1
                """,
                config.auto_describe_batch,
            )
            for r in rows:
                await _auto_describe_one(app, pool, r["global_id"], r["image_path"])
                # Short pause between in-tick calls so provider rate
                # limits stay happy.
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("auto-describe loop tick failed; retrying next interval")
        await asyncio.sleep(config.auto_describe_interval_s)
