"""Immich client — a *reference photo* source for identity enrollment.

What this is NOT: a recognition engine. Immich's face stack (InsightFace
`buffalo_l`: SCRFD detect → align 112×112 → ArcFace 512-d) is architecturally
the same thing BABA already runs — `core/src/baba_core/face_detectors.py` even
cites immich-app's HuggingFace mirror as the download source for the SCRFD BYOM
weights. Routing BABA's per-frame embedding through Immich would buy no accuracy
and would cost a cross-host network hop on a real-time NVR, plus `buffalo_l` is
non-commercial where AuraFace is Apache-2.0. So we don't.

What Immich actually has that BABA lacks is *labeled face variety*. A photo
library holds dozens of named, varied shots per person — angles, years, lighting,
glasses on and off — where a freshly-enrolled BABA identity typically has one or
two portraits. Matching is MIN-over-references, so reference variety is exactly
the lever that moves recall on a hard CCTV crop.

We import PIXELS, never vectors. Immich's `w600k_r50` embedding space is not
comparable to AuraFace's (see `core/src/baba_core/face_models.py`) — cross-
comparing them returns noise. Every imported photo is re-embedded by BABA's own
active face model, so it lands in the same space as live track embeddings.

Why we go through `/api/faces` instead of just downloading assets and running our
own detector over them: a family photo contains five people. `FaceStack.
embed_from_crop` picks the LARGEST face in whatever it's handed
(`core/src/baba_core/face.py` — YuNet sorts by area), so handing it a group shot
would enroll whoever stood closest to the camera under the operator's chosen
name, silently and with a confident-looking embedding. Immich already stores a
per-face bounding box tagged with the person id. We use that box to crop the
*right* face, then let our own detector re-find and align it inside that crop —
which keeps the preprocessing identical to the live path rather than trusting
Immich's landmarks.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)

# app_settings key holding {"base_url": ..., "api_key": ...}. The generic kv
# table is the right home per its own migration note (051): one-off operator
# connection settings don't each deserve a table.
IMMICH_SETTINGS_KEY = "immich"

# Immich serves previews at 1440px long edge by default. A face in a family
# photo lands at 100-300px there — comfortably above MIN_FACE_SIDE_PX — while
# `original` could be a 40 MB HEIC/RAW we'd download only to downscale.
_PREVIEW_SIZE = "preview"

# Per-request page size when walking a person's assets. Immich caps search
# results at 1000/page; 250 keeps each response small enough that a slow link
# doesn't stall the import behind one huge JSON body.
_ASSET_PAGE_SIZE = 250

_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)


class ImmichError(RuntimeError):
    """Immich is unreachable, rejected our key, or answered nonsense.

    Raised rather than returning a sentinel so the enrollment path fails loud:
    a silently-empty import looks identical to "this person has no photos",
    which would send the operator hunting through Immich for a problem that's
    actually on our side.
    """


@dataclass(slots=True, frozen=True)
class ImmichConfig:
    base_url: str
    api_key: str

    @staticmethod
    def from_row(value: dict[str, Any] | None, secret_key: str) -> ImmichConfig | None:
        """Parse the app_settings jsonb payload. Returns None when Immich has
        never been configured, which callers surface as a 503 with setup
        instructions rather than an error.

        The key is stored Fernet-encrypted (`api_key_encrypted`); a plaintext
        `api_key` is a legacy row written before encryption and is accepted so
        an existing deployment keeps working until the operator next saves,
        which re-writes it encrypted. A ciphertext that won't decrypt raises —
        a wrong/rotated secret must fail loud, not silently look unconfigured."""
        from baba_api.crypto import decrypt_secret

        if not value:
            return None
        base_url = str(value.get("base_url") or "").strip().rstrip("/")
        enc = value.get("api_key_encrypted")
        if enc:
            api_key = decrypt_secret(str(enc).encode("ascii"), secret_key).strip()
        else:
            api_key = str(value.get("api_key") or "").strip()
        if not base_url or not api_key:
            return None
        return ImmichConfig(base_url=base_url, api_key=api_key)


@dataclass(slots=True, frozen=True)
class ImmichPerson:
    id: str
    name: str
    thumbnail_path: str | None


@dataclass(slots=True, frozen=True)
class ImmichFaceBox:
    """One detected face inside one asset, in that asset's own pixel space.

    `image_width` / `image_height` are the dimensions Immich's ML saw, which is
    NOT necessarily the preview we download — hence both are carried so the
    caller can rescale the box onto whatever it actually fetched.
    """

    person_id: str | None
    x1: float
    y1: float
    x2: float
    y2: float
    image_width: int
    image_height: int

    @property
    def width(self) -> float:
        return self.x2 - self.x1

    @property
    def height(self) -> float:
        return self.y2 - self.y1


class ImmichClient:
    """Thin async wrapper over the handful of Immich endpoints we need.

    Deliberately not a general-purpose SDK — four calls, no caching, no retry
    beyond httpx's connect default. An import is an operator-initiated
    background job; if Immich is down the right answer is to say so and let
    them re-run it, not to paper over it.
    """

    def __init__(self, config: ImmichConfig) -> None:
        self._config = config
        self._client = httpx.AsyncClient(
            base_url=config.base_url,
            headers={"x-api-key": config.api_key, "Accept": "application/json"},
            timeout=_TIMEOUT,
            follow_redirects=True,
        )

    async def __aenter__(self) -> ImmichClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self._client.aclose()

    async def _get(self, path: str, **params: Any) -> Any:
        try:
            r = await self._client.get(path, params=params or None)
        except httpx.HTTPError as e:
            raise ImmichError(f"Immich unreachable at {self._config.base_url}: {e}") from e
        return self._unwrap(r, path)

    async def _post(self, path: str, payload: dict[str, Any]) -> Any:
        try:
            r = await self._client.post(path, json=payload)
        except httpx.HTTPError as e:
            raise ImmichError(f"Immich unreachable at {self._config.base_url}: {e}") from e
        return self._unwrap(r, path)

    def _unwrap(self, r: httpx.Response, path: str) -> Any:
        if r.status_code == 401:
            raise ImmichError(
                "Immich rejected the API key (401). Generate a fresh one in "
                "Immich → Account Settings → API Keys and re-save it in BABA."
            )
        if r.status_code >= 400:
            raise ImmichError(f"Immich {path} returned HTTP {r.status_code}: {r.text[:200]}")
        try:
            return r.json()
        except ValueError as e:
            raise ImmichError(f"Immich {path} returned non-JSON: {r.text[:200]}") from e

    async def ping(self) -> str:
        """Verify connectivity + key. Returns the server version string.

        Two calls on purpose: `/api/server/version` is unauthenticated (it would
        answer 200 with a bogus key and give a false green light), so we follow
        it with an authenticated `/api/people` HEAD-equivalent to actually prove
        the key works.
        """
        version = await self._get("/api/server/version")
        v = ".".join(str(version.get(k, "?")) for k in ("major", "minor", "patch"))
        await self._get("/api/people", size=1, withHidden=False)
        return v

    async def list_people(self, *, with_hidden: bool = False) -> list[ImmichPerson]:
        """All NAMED people. Immich creates a person cluster for every face it
        finds, most of which are unnamed strangers/passers-by in the background
        of photos — those are useless as enrollment sources (we'd be importing
        an anonymous face under an anonymous identity), so they're filtered out
        here rather than making every caller remember to."""
        out: list[ImmichPerson] = []
        page = 1
        while True:
            data = await self._get("/api/people", page=page, size=500, withHidden=with_hidden)
            for p in data.get("people") or []:
                name = (p.get("name") or "").strip()
                if not name:
                    continue
                out.append(
                    ImmichPerson(
                        id=str(p["id"]),
                        name=name,
                        thumbnail_path=p.get("thumbnailPath"),
                    )
                )
            if not data.get("hasNextPage"):
                break
            page += 1
        out.sort(key=lambda p: p.name.lower())
        return out

    async def get_person(self, person_id: str) -> ImmichPerson | None:
        """One person by id. Returns None when Immich doesn't know the id, or
        when the cluster exists but is unnamed — an unnamed cluster carries no
        information we could enroll (we'd be filing an anonymous face under an
        anonymous identity), so callers treat both cases the same."""
        try:
            data = await self._get(f"/api/people/{person_id}")
        except ImmichError as e:
            if "HTTP 400" in str(e) or "HTTP 404" in str(e):
                return None
            raise
        name = (data.get("name") or "").strip()
        if not name:
            return None
        return ImmichPerson(id=str(data["id"]), name=name, thumbnail_path=data.get("thumbnailPath"))

    async def asset_ids_for_person(self, person_id: str, *, limit: int) -> list[str]:
        """Image asset ids featuring this person, newest first.

        Videos are skipped: `/api/faces` boxes reference a still frame we can't
        address, and a video thumbnail isn't guaranteed to contain the face.
        """
        out: list[str] = []
        page: int | None = 1
        while page is not None and len(out) < limit:
            data = await self._post(
                "/api/search/metadata",
                {
                    "personIds": [person_id],
                    "page": page,
                    "size": _ASSET_PAGE_SIZE,
                    "withExif": False,
                },
            )
            assets = data.get("assets") or {}
            for a in assets.get("items") or []:
                if (a.get("type") or "").upper() != "IMAGE":
                    continue
                out.append(str(a["id"]))
                if len(out) >= limit:
                    break
            nxt = assets.get("nextPage")
            page = int(nxt) if nxt else None
        return out

    async def faces_for_asset(self, asset_id: str) -> list[ImmichFaceBox]:
        data = await self._get("/api/faces", id=asset_id)
        out: list[ImmichFaceBox] = []
        for f in data or []:
            person = f.get("person") or {}
            try:
                out.append(
                    ImmichFaceBox(
                        person_id=str(person["id"]) if person.get("id") else None,
                        x1=float(f["boundingBoxX1"]),
                        y1=float(f["boundingBoxY1"]),
                        x2=float(f["boundingBoxX2"]),
                        y2=float(f["boundingBoxY2"]),
                        image_width=int(f["imageWidth"]),
                        image_height=int(f["imageHeight"]),
                    )
                )
            except (KeyError, TypeError, ValueError):
                # A face row missing its geometry is unusable but shouldn't sink
                # the whole import — skip it and keep going.
                log.warning("immich: unusable face row on asset %s", asset_id)
        return out

    async def download_preview(self, asset_id: str) -> bytes:
        try:
            r = await self._client.get(
                f"/api/assets/{asset_id}/thumbnail",
                params={"size": _PREVIEW_SIZE},
                headers={"Accept": "image/*"},
            )
        except httpx.HTTPError as e:
            raise ImmichError(f"Immich preview fetch failed for {asset_id}: {e}") from e
        if r.status_code >= 400:
            raise ImmichError(f"Immich preview for {asset_id} returned HTTP {r.status_code}")
        return r.content


async def load_config(pool: Any, secret_key: str) -> ImmichConfig | None:
    """Read the Immich connection settings out of app_settings."""
    import json

    raw = await pool.fetchval("SELECT value FROM app_settings WHERE key = $1", IMMICH_SETTINGS_KEY)
    if raw is None:
        return None
    value = json.loads(raw) if isinstance(raw, str) else raw
    return ImmichConfig.from_row(value, secret_key)
