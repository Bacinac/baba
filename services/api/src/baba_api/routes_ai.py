"""AI assistant settings + helpers.

Routes:
  GET    /ai/settings                — list configured providers (key masked)
  PUT    /ai/settings                — upsert credentials for one provider
  DELETE /ai/settings/{provider}     — clear credentials
  POST   /ai/test                    — ping the provider (test API key/model)
  POST   /ai/suggest-zones           — given a camera, ask the model to
                                       propose zones for that view

The frontend uses GET to populate the form (key is shown masked, never
plaintext), PUT to save, and POST /ai/test for the "Test" button.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import time
from pathlib import Path
from uuid import UUID

import httpx
import numpy as np
from fastapi import APIRouter, HTTPException, Request
from home_core.tasks import spawn
from PIL import Image

from baba_api.ai_provider import (
    AiCallResult,
    anthropic_ping,
    anthropic_text_json,
    anthropic_vision_json,
    openai_ping,
    openai_text_json,
    openai_vision_json,
)
from baba_api.crypto import decrypt_secret, encrypt_secret, mask_key
from baba_api.models import (
    ANTHROPIC_DEFAULT_MODEL,
    OPENAI_DEFAULT_MODEL,
    SUPPORTED_AI_PROVIDERS,
    AiSettingsIn,
    AiSettingsOut,
    AiTestIn,
    AiTestOut,
    ClassifyPolygonIn,
    ClassifyPolygonOut,
    ProposedClassRule,
    ProposedZoneRules,
    SegmentAtPointIn,
    SegmentAtPointOut,
    SuggestZonesOut,
    TuneZoneRulesIn,
    TuneZoneRulesOut,
    ZoneSuggestion,
)

log = logging.getLogger(__name__)

ai_router = APIRouter()


def _pool(request: Request):
    return request.app.state.pool


def _secret(request: Request) -> str:
    return request.app.state.secret_key


def _validate_provider(provider: str) -> None:
    if provider not in SUPPORTED_AI_PROVIDERS:
        raise HTTPException(400, f"unsupported provider: {provider!r}")


async def _load_setting_row(request: Request, provider: str):
    return await _pool(request).fetchrow(
        """
        SELECT provider, model, api_key_encrypted, enabled,
               last_used_at, created_at, updated_at
        FROM ai_settings WHERE provider = $1
        """,
        provider,
    )


def _row_to_out(row, secret: str) -> AiSettingsOut:
    plaintext = decrypt_secret(bytes(row["api_key_encrypted"]), secret)
    return AiSettingsOut(
        provider=row["provider"],
        model=row["model"],
        api_key_masked=mask_key(plaintext),
        enabled=row["enabled"],
        last_used_at=row["last_used_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@ai_router.get("/ai/settings", response_model=list[AiSettingsOut])
async def list_ai_settings(request: Request) -> list[AiSettingsOut]:
    rows = await _pool(request).fetch(
        """
        SELECT provider, model, api_key_encrypted, enabled,
               last_used_at, created_at, updated_at
        FROM ai_settings ORDER BY provider
        """,
    )
    secret = _secret(request)
    return [_row_to_out(r, secret) for r in rows]


@ai_router.put("/ai/settings", response_model=AiSettingsOut)
async def upsert_ai_setting(payload: AiSettingsIn, request: Request) -> AiSettingsOut:
    _validate_provider(payload.provider)
    secret = _secret(request)
    existing = await _load_setting_row(request, payload.provider)

    if payload.api_key:
        ciphertext = encrypt_secret(payload.api_key, secret)
    elif existing:
        ciphertext = bytes(existing["api_key_encrypted"])
    else:
        raise HTTPException(400, "api_key is required when creating a new provider entry")

    row = await _pool(request).fetchrow(
        """
        INSERT INTO ai_settings (provider, model, api_key_encrypted, enabled)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (provider) DO UPDATE SET
            model = EXCLUDED.model,
            api_key_encrypted = EXCLUDED.api_key_encrypted,
            enabled = EXCLUDED.enabled
        RETURNING provider, model, api_key_encrypted, enabled,
                  last_used_at, created_at, updated_at
        """,
        payload.provider,
        payload.model,
        ciphertext,
        payload.enabled,
    )
    return _row_to_out(row, secret)


@ai_router.delete("/ai/settings/{provider}", status_code=204)
async def delete_ai_setting(provider: str, request: Request) -> None:
    _validate_provider(provider)
    res = await _pool(request).execute("DELETE FROM ai_settings WHERE provider = $1", provider)
    if res.endswith(" 0"):
        raise HTTPException(404, "provider not configured")


async def _ping(provider: str, api_key: str, model: str) -> AiCallResult:
    if provider == "anthropic":
        return await anthropic_ping(api_key, model)
    if provider == "openai":
        return await openai_ping(api_key, model)
    raise HTTPException(400, f"unsupported provider: {provider!r}")


async def _vision_json(
    provider: str,
    api_key: str,
    model: str,
    *,
    system: str,
    user_text: str,
    image_jpeg: bytes,
) -> AiCallResult:
    if provider == "anthropic":
        return await anthropic_vision_json(
            api_key,
            model,
            system=system,
            user_text=user_text,
            image_jpeg=image_jpeg,
        )
    if provider == "openai":
        return await openai_vision_json(
            api_key,
            model,
            system=system,
            user_text=user_text,
            image_jpeg=image_jpeg,
        )
    raise HTTPException(400, f"unsupported provider: {provider!r}")


@ai_router.post("/ai/test", response_model=AiTestOut)
async def test_ai_credentials(payload: AiTestIn, request: Request) -> AiTestOut:
    _validate_provider(payload.provider)
    secret = _secret(request)
    api_key = payload.api_key
    model = payload.model
    if not api_key or not model:
        row = await _load_setting_row(request, payload.provider)
        if row is None:
            raise HTTPException(400, "no stored credentials; pass api_key and model")
        if not api_key:
            api_key = decrypt_secret(bytes(row["api_key_encrypted"]), secret)
        if not model:
            model = row["model"]

    result = await _ping(payload.provider, api_key, model)
    if result.ok:
        await _pool(request).execute(
            "UPDATE ai_settings SET last_used_at = now() WHERE provider = $1",
            payload.provider,
        )
    return AiTestOut(
        ok=result.ok,
        model=result.model or model,
        latency_ms=result.latency_ms,
        error=result.error,
    )


# --- Suggest zones --------------------------------------------------------

_SUGGEST_SYSTEM = (
    "You are an assistant helping a video surveillance operator design "
    "zones for a fixed-position camera. You are shown one frame from the "
    "camera. Propose at most 4 useful zones for security/analytics rules — "
    "fewer is better than forcing a marginal zone. Each zone is a closed "
    "polygon over the image. "
    "Polygon coordinates MUST be normalized to the image: x in [0,1] from "
    "left edge, y in [0,1] from top edge. Use 4–10 vertices per polygon, "
    "ordered (clockwise or counter-clockwise; we close it for you). "
    "Output ONLY a JSON object — no prose, no markdown fences — matching this schema:\n"
    '{"zones": [{"name": str, "kind": one of '
    '["entry","exit","restricted","parking","no_go","interest","generic"], '
    '"polygon": [[x,y], …], "rationale": str (short, the user\'s language)}]}\n'
    "HARD RULES:\n"
    "  1. Zones MUST NOT overlap each other. If two regions are adjacent, "
    "leave a small visible gap between their polygons.\n"
    "  2. Each polygon must cover ONLY its named feature — do not extend a "
    "polygon into empty pavement, sky, or unrelated areas to fill space.\n"
    "  3. Prefer obvious functional areas (doorways, walkways, parking "
    "spots, fenced/off-limits regions). Skip vague 'general area' zones.\n"
    "  4. Rank by importance and return the top zones first. If only one "
    "zone is clearly useful, return only that one.\n"
    "Keep names short (1–3 words). If nothing is sensible (e.g. blank frame), "
    "return an empty zones array."
)


def _build_user_prompt(camera_name: str, locale: str) -> str:
    lang = "Croatian" if locale.startswith("hr") else "English"
    return (
        f"Camera: {camera_name}. Propose zones for this view. "
        f"Write the `name` and `rationale` fields in {lang}."
    )


_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _json_from_vlm(text: str) -> object | None:
    """Parse a JSON value out of a VLM text response: strip a ```json fence if
    the model added one, then json.loads; on failure retry on the first {...}
    block. Returns the parsed value (dict/list/…) or None if nothing parses.
    Shared by the zones/classify/tune parsers below."""
    if not text:
        return None
    m = _JSON_FENCE.search(text)
    raw = m.group(1) if m else text
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Last-resort: try to find the first {...} block.
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            return json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return None


def _parse_zones_response(text: str) -> list[ZoneSuggestion]:
    data = _json_from_vlm(text)
    zones = data.get("zones") if isinstance(data, dict) else None
    if not isinstance(zones, list):
        return []

    out: list[ZoneSuggestion] = []
    for z in zones:
        if not isinstance(z, dict):
            continue
        poly_raw = z.get("polygon")
        if not isinstance(poly_raw, list) or len(poly_raw) < 3:
            continue
        polygon: list[list[float]] = []
        for pt in poly_raw:
            if not (isinstance(pt, (list, tuple)) and len(pt) >= 2):
                continue
            try:
                x = float(pt[0])
                y = float(pt[1])
            except (TypeError, ValueError):
                continue
            # Clip to [0,1] rather than reject: the model is often slightly off.
            polygon.append([max(0.0, min(1.0, x)), max(0.0, min(1.0, y))])
        if len(polygon) < 3:
            continue
        out.append(
            ZoneSuggestion(
                name=str(z.get("name") or "Zone")[:64],
                kind=str(z.get("kind") or "generic")[:32],
                polygon=polygon,
                rationale=str(z.get("rationale") or "")[:300],
            )
        )
    return out


# Color palette assigned round-robin to suggestions for visual distinction.
_SUGGEST_COLORS = (
    "#f59e0b",
    "#10b981",
    "#3b82f6",
    "#ec4899",
    "#a855f7",
    "#ef4444",
)


# ---------------------------------------------------------------------------
# SAM2 refinement: turn the VLM's rough polygons into pixel-perfect masks
# by re-segmenting each region with Segment Anything 2. The VLM is good at
# semantic identification ("this is a gate") but bad at edge precision;
# SAM2 takes the rough region as a prompt and snaps to actual boundaries.
#
# Lazy-initialized and cached on app.state to amortize the ~150MB encoder
# load. Single instance shared across requests — ORT sessions are
# thread-safe for `.run()` so concurrent suggest-zones calls can share it.
# ---------------------------------------------------------------------------


def _get_sam2(request: Request):
    """Return a cached `Sam2Predictor` or None if SAM2 isn't configured /
    failed to load. The first call constructs and caches it on app.state;
    subsequent calls hit the cache."""
    app = request.app
    if hasattr(app.state, "_sam2"):
        return app.state._sam2
    cfg = app.state.config
    if not cfg.sam2_refine_enabled:
        log.info("sam2: refinement disabled by BABA_SAM2_REFINE=0")
        app.state._sam2 = None
        return None
    enc, dec = cfg.sam2_encoder_path, cfg.sam2_decoder_path
    if not (enc and dec and os.path.isfile(enc) and os.path.isfile(dec)):
        log.info(
            "sam2: refinement disabled (encoder=%r decoder=%r — missing or unset)",
            enc,
            dec,
        )
        app.state._sam2 = None
        return None
    try:
        from baba_core.sam2 import Sam2Predictor

        app.state._sam2 = Sam2Predictor(Path(enc), Path(dec))
    except Exception as e:
        log.warning("sam2: init failed, refinement disabled: %s", e, exc_info=True)
        app.state._sam2 = None
    return app.state._sam2


def _jpeg_to_rgb(jpeg: bytes) -> np.ndarray:
    """Decode a JPEG into a contiguous uint8 HWC RGB numpy array."""
    img = Image.open(io.BytesIO(jpeg))
    if img.mode != "RGB":
        img = img.convert("RGB")
    return np.asarray(img, dtype=np.uint8)


def _refine_with_sam2_sync(
    predictor,
    snapshot_rgb: np.ndarray,
    vlm_polygons: list[list[list[float]]],
) -> tuple[list[list[list[float]]], int, int]:
    """Blocking SAM2 refinement of N polygons. Runs in a worker thread.
    Returns (refined_polygons, accepted_count, rejected_count). Each
    rejected polygon falls back to the original VLM coords so the user
    still sees a suggestion."""
    from baba_core.sam2 import mask_to_polygon

    predictor.set_image(snapshot_rgb)
    refined: list[list[list[float]]] = []
    accepted = 0
    rejected = 0
    for vp in vlm_polygons:
        try:
            mask = predictor.predict_from_polygon(vp, use_polygon_as_mask=True)
        except Exception as e:
            log.warning(
                "sam2: predict failed for polygon, keeping VLM original: %s", e, exc_info=True
            )
            rejected += 1
            refined.append(vp)
            continue
        if mask is None:
            rejected += 1
            refined.append(vp)
            continue
        new_poly = mask_to_polygon(mask)
        if new_poly:
            accepted += 1
            refined.append(new_poly)
        else:
            rejected += 1
            refined.append(vp)
    return refined, accepted, rejected


async def _fetch_snapshot(
    go2rtc_url: str, slug: str, auth: tuple[str, str] | None = None
) -> bytes:
    """Pull a JPEG snapshot from go2rtc for `slug`. go2rtc exposes
    /api/frame.jpeg?src=<name> on its API port."""
    url = f"{go2rtc_url.rstrip('/')}/api/frame.jpeg"
    async with httpx.AsyncClient(timeout=10, auth=auth) as client:
        r = await client.get(url, params={"src": slug})
    if r.status_code != 200:
        raise HTTPException(502, f"snapshot fetch failed: {r.status_code}")
    if not r.content:
        raise HTTPException(502, "snapshot empty")
    return r.content


# Cap the snapshot at ~3 MP before sending to a VLM. This handles 4K and
# 8MP cameras while leaving 1080p, 5MP portrait (1920×2560), and similar
# common doorbell/IP resolutions untouched — re-encoding a JPEG always
# loses some quality (generation loss) and that hurt low-light scenes more
# than the resize helped. Providers do their own internal resize anyway;
# our job here is just to avoid sending a needlessly huge payload.
_VLM_MAX_PIXELS = 3_000_000  # ≈3 MP — 1080p (2 MP) and 5MP portrait pass through
_VLM_LONG_EDGE = 1920  # target long edge IF we do resize
_VLM_JPEG_QUALITY = 92


def _downscale_for_vlm(jpeg: bytes) -> bytes:
    """Return a JPEG ≤ `_VLM_MAX_PIXELS` so we don't ship a 4K snapshot
    just to have the provider downscale it server-side. For images already
    under the cap (1080p, 5MP portrait) we return the original bytes
    untouched — re-encoding always costs a generation of JPEG quality."""
    try:
        img = Image.open(io.BytesIO(jpeg))
        img.load()
    except (OSError, Image.DecompressionBombError):
        # If Pillow can't read it (shouldn't happen — go2rtc produces clean
        # JPEGs), let the provider deal with the raw bytes.
        return jpeg
    w, h = img.size
    if w * h <= _VLM_MAX_PIXELS:
        return jpeg
    scale = _VLM_LONG_EDGE / max(w, h)
    new_size = (max(1, round(w * scale)), max(1, round(h * scale)))
    resized = img.resize(new_size, Image.Resampling.LANCZOS)
    if resized.mode != "RGB":
        resized = resized.convert("RGB")
    buf = io.BytesIO()
    resized.save(buf, format="JPEG", quality=_VLM_JPEG_QUALITY, optimize=True)
    return buf.getvalue()


async def pick_active_provider(pool, secret: str, requested: str | None = None) -> tuple[str, str, str]:
    """Resolve which provider to use for an AI call — Request-free so
    background tasks (telemetry auto-analyzer) can share it.

    - If `requested` is given, validate and use that row (must be enabled).
    - Otherwise pick the first enabled row in `SUPPORTED_AI_PROVIDERS` order.

    Returns (provider, api_key_plaintext, model). Raises 400 if nothing usable.
    """

    async def _row(provider: str):
        return await pool.fetchrow(
            "SELECT model, api_key_encrypted, enabled FROM ai_settings WHERE provider = $1",
            provider,
        )

    if requested:
        _validate_provider(requested)
        row = await _row(requested)
        if row is None or not row["enabled"]:
            raise HTTPException(400, f"{requested!r} is not configured — open Settings → AI")
        return requested, decrypt_secret(bytes(row["api_key_encrypted"]), secret), row["model"]

    # No request — pick the first enabled provider deterministically.
    for p in SUPPORTED_AI_PROVIDERS:
        row = await _row(p)
        if row is not None and row["enabled"]:
            return p, decrypt_secret(bytes(row["api_key_encrypted"]), secret), row["model"]
    raise HTTPException(400, "no AI credentials configured — open Settings → AI to set them")


async def _pick_active_provider(request: Request, requested: str | None) -> tuple[str, str, str]:
    return await pick_active_provider(_pool(request), _secret(request), requested)


@ai_router.post("/ai/suggest-zones/{camera_id}", response_model=SuggestZonesOut)
async def suggest_zones(
    camera_id: UUID,
    request: Request,
    locale: str = "hr",
    provider: str | None = None,
) -> SuggestZonesOut:
    pool = _pool(request)
    cam = await pool.fetchrow("SELECT slug, name FROM cameras WHERE id = $1", camera_id)
    if cam is None:
        raise HTTPException(404, "camera not found")

    active_provider, api_key, model = await _pick_active_provider(request, provider)

    go2rtc_url = request.app.state.config.go2rtc_url
    try:
        snapshot = await _fetch_snapshot(
            go2rtc_url, cam["slug"], request.app.state.config.go2rtc_auth
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"snapshot fetch failed: {e}") from e

    # Resize to the model's native processing grid before sending. Coords come
    # back normalized [0,1] so the frontend doesn't need to know about this.
    snapshot = await asyncio.to_thread(_downscale_for_vlm, snapshot)

    result = await _vision_json(
        active_provider,
        api_key,
        model,
        system=_SUGGEST_SYSTEM,
        user_text=_build_user_prompt(cam["name"], locale),
        image_jpeg=snapshot,
    )
    spawn(
        pool.execute(
            "UPDATE ai_settings SET last_used_at = now() WHERE provider = $1",
            active_provider,
        )
    )

    if not result.ok:
        return SuggestZonesOut(
            model=model,
            suggestions=[],
            latency_ms=result.latency_ms,
            error=result.error,
        )

    suggestions = _parse_zones_response(result.text or "")
    if not suggestions:
        # Empty parse — log the raw text so we can see whether the model
        # returned valid JSON we mis-parsed, an empty JSON object, or
        # something completely off-format. Truncate to avoid spamming.
        log.info(
            "suggest-zones: provider=%s model=%s parsed 0 zones from %d-char response: %r",
            active_provider,
            result.model or model,
            len(result.text or ""),
            (result.text or "")[:400],
        )

    # SAM2 refinement: turn each VLM rough polygon into a pixel-perfect
    # one. We pass the (possibly downscaled) snapshot — same image the
    # VLM saw, so its coords map directly without an extra transform.
    # On any failure (model missing, ORT init, per-polygon glitch) we
    # silently fall back to the VLM polygon so the user still gets a
    # suggestion, just less precise.
    # First use reads and compiles two ONNX models for the Arc. Doing that
    # inline froze the whole api — every request, every SSE stream — for as
    # long as the compile took.
    sam2 = await asyncio.to_thread(_get_sam2, request)
    if sam2 is not None and suggestions:
        try:
            rgb = await asyncio.to_thread(_jpeg_to_rgb, snapshot)
            t0 = time.perf_counter()
            refined, accepted, rejected = await asyncio.to_thread(
                _refine_with_sam2_sync,
                sam2,
                rgb,
                [s.polygon for s in suggestions],
            )
            log.info(
                "sam2: refined %d/%d polygons (rejected %d) in %.0fms",
                accepted,
                len(refined),
                rejected,
                (time.perf_counter() - t0) * 1000,
            )
            for s, new_poly in zip(suggestions, refined, strict=True):
                s.polygon = new_poly
        except Exception as e:
            log.warning("sam2: refinement skipped (%s)", e, exc_info=True)

    for i, s in enumerate(suggestions):
        s.color = _SUGGEST_COLORS[i % len(_SUGGEST_COLORS)]

    return SuggestZonesOut(
        model=result.model or model,
        suggestions=suggestions,
        latency_ms=result.latency_ms,
        error=None,
    )


# ---------------------------------------------------------------------------
# User-driven SAM2 segmentation. Click a point on the snapshot, get back a
# pixel-perfect polygon of whatever's under that point. Optional negative
# clicks carve out unwanted regions; `prev_polygon` enables iterative
# refinement so subsequent clicks adjust the existing mask.
#
# This is SAM2's strongest mode (matches Meta's own interactive demo). The
# bulk-`suggest-zones` path stays available for "rough draft please" but
# users who want accurate zones should use this click flow instead.
# ---------------------------------------------------------------------------


def _polygon_to_bool_mask(
    poly_norm: list[list[float]],
    h: int,
    w: int,
) -> np.ndarray:
    import cv2

    canvas = np.zeros((h, w), dtype=np.uint8)
    pts = np.asarray(
        [[round(x * w), round(y * h)] for x, y in poly_norm],
        dtype=np.int32,
    )
    cv2.fillPoly(canvas, [pts], color=1)
    return canvas.astype(bool)


def _segment_at_point_sync(
    predictor,
    snapshot_rgb: np.ndarray,
    payload: SegmentAtPointIn,
) -> list[list[float]] | None:
    from baba_core.sam2 import mask_to_polygon

    predictor.set_image(snapshot_rgb)
    h, w = snapshot_rgb.shape[:2]
    prev_mask = _polygon_to_bool_mask(payload.prev_polygon, h, w) if payload.prev_polygon else None
    mask = predictor.predict_from_clicks(
        positive_norm=payload.positive,
        negative_norm=payload.negative,
        prev_mask=prev_mask,
    )
    if mask is None:
        return None
    return mask_to_polygon(mask)


@ai_router.post(
    "/ai/segment-at-point/{camera_id}",
    response_model=SegmentAtPointOut,
)
async def segment_at_point(
    camera_id: UUID,
    payload: SegmentAtPointIn,
    request: Request,
) -> SegmentAtPointOut:
    if not payload.positive:
        raise HTTPException(400, "at least one positive click required")

    pool = _pool(request)
    cam = await pool.fetchrow(
        "SELECT slug FROM cameras WHERE id = $1",
        camera_id,
    )
    if cam is None:
        raise HTTPException(404, "camera not found")

    # First use reads and compiles two ONNX models for the Arc. Doing that
    # inline froze the whole api — every request, every SSE stream — for as
    # long as the compile took.
    sam2 = await asyncio.to_thread(_get_sam2, request)
    if sam2 is None:
        raise HTTPException(
            503,
            "SAM2 is not available — set BABA_SAM2_ENCODER_MODEL / "
            "BABA_SAM2_DECODER_MODEL and ensure the model files exist",
        )

    go2rtc_url = request.app.state.config.go2rtc_url
    try:
        snapshot = await _fetch_snapshot(
            go2rtc_url, cam["slug"], request.app.state.config.go2rtc_auth
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"snapshot fetch failed: {e}") from e

    t0 = time.perf_counter()
    rgb = await asyncio.to_thread(_jpeg_to_rgb, snapshot)
    try:
        polygon = await asyncio.to_thread(
            _segment_at_point_sync,
            sam2,
            rgb,
            payload,
        )
    except Exception as e:
        log.exception("sam2: segment-at-point failed")
        return SegmentAtPointOut(
            polygon=[],
            latency_ms=int((time.perf_counter() - t0) * 1000),
            error=str(e),
        )
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    if polygon is None:
        return SegmentAtPointOut(
            polygon=[],
            latency_ms=elapsed_ms,
            error="SAM2 produced no usable mask for this point",
        )
    return SegmentAtPointOut(polygon=polygon, latency_ms=elapsed_ms, error=None)


# ---------------------------------------------------------------------------
# Claude-driven polygon classification. The user has a polygon (either
# AI-suggested, SAM2-segmented, or hand-drawn) and wants a name + kind for
# it. We crop the snapshot to the polygon's bbox + small margin, send to
# the VLM with a tight prompt, parse the response into our zone schema.
# Cheap and avoids forcing the user to invent names for everything.
# ---------------------------------------------------------------------------

_CLASSIFY_SYSTEM = (
    "You are helping a video-surveillance operator label a region they "
    "drew on a camera frame. You will be shown the full frame plus a "
    "small crop highlighting the region. Output ONLY a JSON object — "
    "no prose, no markdown fences — with these fields:\n"
    '{"name": str (1-3 words, the user\'s language), '
    '"kind": one of ["entry","exit","restricted","parking",'
    '"no_go","interest","generic"], '
    '"rationale": str (one short sentence, the user\'s language)}\n'
    "Be specific: prefer 'Front Gate' over 'Gate', 'Driveway' over 'Path'. "
    "Pick the kind that best matches typical use: entry/exit for doors and "
    "gates, parking for parking spots, no_go for off-limits regions nobody "
    "should be in (a roof, a machine bay), restricted for fenced private "
    "areas, interest for walkways and approach paths, generic only when "
    "nothing else fits."
    # Was: "no_go for neighbour property". That reads as "don't watch this",
    # which no_go has never done — every kind here only ADDS events, so the
    # suggestion promised suppression the pipeline would not deliver. Ground
    # the operator wants unwatched is the `ignore` kind, which the model is
    # deliberately not allowed to propose (see _classify below).
)


def _crop_with_polygon_overlay(jpeg: bytes, polygon: list[list[float]]) -> bytes:
    """Render a JPEG that's the original frame with the polygon outlined
    in bright red. Sent to the VLM so it has both spatial context (the
    whole scene) and a clear pointer to which region we're asking about."""
    import cv2

    img = Image.open(io.BytesIO(jpeg))
    if img.mode != "RGB":
        img = img.convert("RGB")
    arr = np.asarray(img, dtype=np.uint8).copy()
    h, w = arr.shape[:2]
    pts = np.asarray(
        [[round(x * w), round(y * h)] for x, y in polygon],
        dtype=np.int32,
    )
    overlay = arr.copy()
    cv2.fillPoly(overlay, [pts], color=(255, 64, 64))
    arr = cv2.addWeighted(overlay, 0.35, arr, 0.65, 0)
    cv2.polylines(arr, [pts], isClosed=True, color=(255, 0, 0), thickness=3)
    out = Image.fromarray(arr, mode="RGB")
    buf = io.BytesIO()
    out.save(buf, format="JPEG", quality=88, optimize=True)
    return buf.getvalue()


def _parse_classify_response(text: str) -> dict:
    data = _json_from_vlm(text)
    return data if isinstance(data, dict) else {}


@ai_router.post(
    "/ai/classify-polygon/{camera_id}",
    response_model=ClassifyPolygonOut,
)
async def classify_polygon(
    camera_id: UUID,
    payload: ClassifyPolygonIn,
    request: Request,
) -> ClassifyPolygonOut:
    if len(payload.polygon) < 3:
        raise HTTPException(400, "polygon must have at least 3 vertices")

    pool = _pool(request)
    cam = await pool.fetchrow(
        "SELECT slug, name FROM cameras WHERE id = $1",
        camera_id,
    )
    if cam is None:
        raise HTTPException(404, "camera not found")

    active_provider, api_key, model = await _pick_active_provider(
        request,
        payload.provider,
    )

    go2rtc_url = request.app.state.config.go2rtc_url
    try:
        snapshot = await _fetch_snapshot(
            go2rtc_url, cam["slug"], request.app.state.config.go2rtc_auth
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"snapshot fetch failed: {e}") from e

    snapshot = await asyncio.to_thread(_downscale_for_vlm, snapshot)
    annotated = await asyncio.to_thread(
        _crop_with_polygon_overlay,
        snapshot,
        payload.polygon,
    )

    lang = "Croatian" if payload.locale.startswith("hr") else "English"
    user_text = (
        f"Camera: {cam['name']}. The highlighted red region is the zone "
        f"the operator drew. Name it (in {lang}) and pick its kind."
    )
    result = await _vision_json(
        active_provider,
        api_key,
        model,
        system=_CLASSIFY_SYSTEM,
        user_text=user_text,
        image_jpeg=annotated,
    )
    spawn(
        pool.execute(
            "UPDATE ai_settings SET last_used_at = now() WHERE provider = $1",
            active_provider,
        )
    )

    if not result.ok:
        return ClassifyPolygonOut(
            name="",
            kind="generic",
            rationale="",
            model=result.model or model,
            latency_ms=result.latency_ms,
            error=result.error,
        )

    data = _parse_classify_response(result.text or "")
    # 'ignore' is deliberately NOT proposable by the model, and the fallback
    # below turns any attempt into 'generic'. The consequences are asymmetric:
    # a wrong 'entry' costs a few spurious events the operator will notice,
    # while a wrong 'ignore' silently blinds that ground — a gap in coverage
    # that shows up as nothing at all. Blinding a camera stays a deliberate
    # human act, made from the Kind dropdown.
    valid_kinds = {"entry", "exit", "restricted", "parking", "no_go", "interest", "generic"}
    kind = str(data.get("kind") or "generic")
    if kind not in valid_kinds:
        kind = "generic"
    return ClassifyPolygonOut(
        name=str(data.get("name") or "")[:64],
        kind=kind,  # type: ignore[arg-type]
        rationale=str(data.get("rationale") or "")[:300],
        model=result.model or model,
        latency_ms=result.latency_ms,
        error=None,
    )


@ai_router.get("/ai/defaults")
async def ai_defaults() -> dict:
    """Frontend convenience: per-provider suggested models for the dropdown.
    The user can always type a different model — these are just the menu."""
    return {
        "providers": list(SUPPORTED_AI_PROVIDERS),
        "anthropic_default_model": ANTHROPIC_DEFAULT_MODEL,
        "anthropic_models": [
            "claude-opus-4-8",
            "claude-sonnet-5",
            "claude-haiku-4-5",
        ],
        "openai_default_model": OPENAI_DEFAULT_MODEL,
        "openai_models": [
            "gpt-5",
            "gpt-5-mini",
            "gpt-5-nano",
            "gpt-4.1",
            "gpt-4o",
        ],
    }


# ---------------------------------------------------------------------------
# Auto-tune zone rules. The client collects N seconds of live detections,
# bucketed per-zone × per-class with confidence/area percentiles, and
# sends the summary here. We ask the configured VLM (Claude/GPT) to
# propose per-class min_confidence + min_area_pct that separate signal
# from noise. The model also returns a short rationale per zone.
#
# Text-only (no image) — the stats are the signal. The model's job is
# inference on numbers, not visual interpretation.
# ---------------------------------------------------------------------------


async def _text_json(
    provider: str,
    api_key: str,
    model: str,
    *,
    system: str,
    user_text: str,
) -> AiCallResult:
    if provider == "anthropic":
        return await anthropic_text_json(
            api_key,
            model,
            system=system,
            user_text=user_text,
        )
    if provider == "openai":
        return await openai_text_json(
            api_key,
            model,
            system=system,
            user_text=user_text,
        )
    raise HTTPException(400, f"unsupported provider: {provider!r}")


_TUNE_SYSTEM = (
    "You are a CCTV operator helping tune detection-zone rules. For each "
    "zone you receive: name, kind, current rules, and a histogram-style "
    "summary of detections observed inside that zone over a short window "
    "(count, confidence percentiles, bbox-area percentile). Propose "
    "per-class rules that minimise false positives without losing real "
    "events.\n\n"
    "Guidelines:\n"
    "  - Pick min_confidence near the OBSERVED p25 only if p25 is already "
    "    above 0.40 — otherwise pick a value somewhere between p25 and "
    "    p50 that still keeps the majority of detections.\n"
    "  - For a sparsely-populated class (count < 5) be conservative; "
    "    favour the current rule unless data clearly disagrees.\n"
    "  - For interest/seating/parking zones favour higher specificity "
    "    (0.55–0.65 min_conf typical). For entry/exit/no_go zones favour "
    "    recall (0.40–0.50).\n"
    "  - Use min_area_pct sparingly — only when small-bbox false positives "
    "    are obvious (very small p50 area combined with low p25 conf).\n"
    "  - If a class is observed less than ~2 times in the window AND the "
    "    zone's kind doesn't require it, drop it from enabled_classes.\n\n"
    "Return ONLY a JSON object with this exact shape:\n"
    '{"proposals": [{"zone_id": "<uuid>", "zone_name": "<name>", '
    '"enabled_classes": {"<class>": {"min_confidence": 0.5, '
    '"min_area_pct": null, "min_dwell_ms": null, "cooldown_s": null}}, '
    '"rationale": "<one short sentence per zone>"}]}'
)


def _parse_tune_response(text: str) -> list[ProposedZoneRules]:
    data = _json_from_vlm(text)
    proposals_raw = data.get("proposals") if isinstance(data, dict) else None
    if not isinstance(proposals_raw, list):
        return []
    out: list[ProposedZoneRules] = []
    for p in proposals_raw:
        if not isinstance(p, dict):
            continue
        enabled = p.get("enabled_classes") or {}
        if not isinstance(enabled, dict):
            continue
        cls_rules: dict[str, ProposedClassRule] = {}
        for cls, rule in enabled.items():
            if not isinstance(rule, dict):
                continue
            try:
                cls_rules[str(cls)[:64]] = ProposedClassRule(
                    min_confidence=_fraction(rule.get("min_confidence")),
                    min_area_pct=_fraction(rule.get("min_area_pct")),
                    min_dwell_ms=_nonneg_int(rule.get("min_dwell_ms")),
                    cooldown_s=_nonneg_int(rule.get("cooldown_s")),
                )
            except ValueError:
                continue
        try:
            out.append(
                ProposedZoneRules(
                    zone_id=str(p.get("zone_id") or "")[:64],
                    zone_name=str(p.get("zone_name") or "Zone")[:64],
                    enabled_classes=cls_rules,
                    rationale=str(p.get("rationale") or "")[:400],
                )
            )
        except ValueError:
            continue
    return out


# These parse LLM output, so they CLAMP as well as coerce — a model that
# proposes min_confidence=5 or a negative dwell must not reach the DB. The
# same four fields are read back out of JSONB by the event-manager
# (`zones._as_float` / `_as_int`), which only coerces: by then the value has
# already passed both this gate and the Pydantic bounds on ZoneClassRule.
def _fraction(v) -> float | None:
    """A confidence / area fraction, clamped into [0, 1]. None when unusable."""
    if v is None or v == "":
        return None
    try:
        f = float(v)
        if f != f:  # NaN
            return None
        return max(0.0, min(1.0, f))
    except (TypeError, ValueError):
        return None


def _nonneg_int(v) -> int | None:
    """A duration / count in its own unit, floored at 0. None when unusable."""
    if v is None or v == "":
        return None
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return None


@ai_router.post("/ai/tune-zone-rules/{camera_id}", response_model=TuneZoneRulesOut)
async def tune_zone_rules(
    camera_id: UUID,
    payload: TuneZoneRulesIn,
    request: Request,
    provider: str | None = None,
) -> TuneZoneRulesOut:
    cam = await _pool(request).fetchrow("SELECT name FROM cameras WHERE id = $1", camera_id)
    if cam is None:
        raise HTTPException(404, "camera not found")

    active_provider, api_key, model = await _pick_active_provider(request, provider)

    # Compact text payload — small enough for any model's context window.
    summary = {
        "camera": cam["name"],
        "frame": [payload.frame_width, payload.frame_height],
        "observation_window_seconds": payload.duration_s,
        "zones": [
            {
                "zone_id": z.zone_id,
                "zone_name": z.zone_name,
                "zone_kind": z.zone_kind,
                "current_rules": z.current_rules,
                "observations_by_class": {
                    cls: {
                        "count": s.count,
                        "conf_p25": round(s.conf_p25, 3),
                        "conf_p50": round(s.conf_p50, 3),
                        "conf_p75": round(s.conf_p75, 3),
                        "conf_max": round(s.conf_max, 3),
                        "area_p50": round(s.area_p50, 4),
                    }
                    for cls, s in z.class_stats.items()
                },
            }
            for z in payload.zones
        ],
    }
    user_text = (
        f"Observation data for tuning. Return a JSON object with "
        f"`proposals` per the system instructions.\n\n"
        f"{json.dumps(summary, ensure_ascii=False, indent=2)}"
    )

    result = await _text_json(
        active_provider,
        api_key,
        model,
        system=_TUNE_SYSTEM,
        user_text=user_text,
    )
    spawn(
        _pool(request).execute(
            "UPDATE ai_settings SET last_used_at = now() WHERE provider = $1",
            active_provider,
        )
    )
    if not result.ok:
        return TuneZoneRulesOut(
            model=model,
            proposals=[],
            latency_ms=result.latency_ms,
            error=result.error,
        )
    proposals = _parse_tune_response(result.text or "")
    if not proposals:
        log.info(
            "tune-zone-rules: provider=%s model=%s parsed 0 proposals from %d-char response: %r",
            active_provider,
            result.model or model,
            len(result.text or ""),
            (result.text or "")[:400],
        )
    return TuneZoneRulesOut(
        model=result.model or model,
        proposals=proposals,
        latency_ms=result.latency_ms,
        error=None,
    )
