"""Client for OPUS · Library, the family photo catalogue BABA enrols people from.

The library holds every named face in the household's photographs: who it is,
where in which photograph it sits, when it was taken, how many pixels it has.
What we take from it is PIXELS — the preview of the photograph and the box the
library drew on it — and never its vectors. Its embedder (AdaFace) and ours
(AuraFace / TopoFR) do not share a space, so every face is re-embedded here by
the active model and lands beside the camera captures it will be compared to.

The box matters as much as the pixels. `FaceStack.embed_from_crop` takes the
LARGEST face it finds and does not say which one that was; a family photograph
has five people in it, and enrolling the wrong one under the operator's name
would be silent and permanent. So the crop is cut around the library's box and
our own detector has to land on that box before anything is embedded.

Auth is the household service token (`X-OPUS-Token`), the same credential the
other OPUS modules carry. Config lives in `app_settings['opus']`.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
from dataclasses import dataclass
from typing import Any

import asyncpg
import httpx

log = logging.getLogger(__name__)

OPUS_SETTINGS_KEY = "opus"
_TOKEN_HEADER = "X-OPUS-Token"
_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)


class OpusError(RuntimeError):
    """Anything that stops us reading the library: unreachable, bad token,
    unexpected answer. Callers translate to 4xx/5xx as appropriate."""


@dataclass(slots=True, frozen=True)
class OpusConfig:
    base_url: str
    token: str

    @classmethod
    def from_row(cls, value: dict[str, Any] | None, secret_key: str) -> OpusConfig | None:
        # Token stored Fernet-encrypted (`token_encrypted`); a plaintext `token`
        # is a legacy row accepted until the operator next saves. A ciphertext
        # that won't decrypt raises rather than looking unconfigured.
        from baba_api.crypto import decrypt_secret

        if not value:
            return None
        base_url = str(value.get("base_url") or "").strip().rstrip("/")
        enc = value.get("token_encrypted")
        if enc:
            token = decrypt_secret(str(enc).encode("ascii"), secret_key).strip()
        else:
            token = str(value.get("token") or "").strip()
        if not base_url or not token:
            return None
        return cls(base_url=base_url, token=token)


@dataclass(slots=True, frozen=True)
class OpusPerson:
    id: int
    name: str
    given_name: str
    faces: int
    first_year: int | None
    last_year: int | None
    cover_face_id: int | None


@dataclass(slots=True, frozen=True)
class OpusFace:
    """One face of one person, as the library keeps it. The box is fractions
    of the frame (0..1), valid at every size of the same photograph; `px` is
    the face's width in the ORIGINAL, the number that says whether there is
    anything to identify."""

    id: int
    photo: str
    taken_at: _dt.datetime | None
    x: float
    y: float
    w: float
    h: float
    px: int
    score: float


class OpusClient:
    def __init__(self, cfg: OpusConfig) -> None:
        self._cfg = cfg
        self._http = httpx.AsyncClient(
            base_url=cfg.base_url,
            headers={_TOKEN_HEADER: cfg.token, "Accept": "application/json"},
            timeout=_TIMEOUT,
            follow_redirects=True,
        )

    async def __aenter__(self) -> OpusClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._http.aclose()

    async def _get(self, path: str, **params: Any) -> Any:
        try:
            r = await self._http.get(path, params={k: v for k, v in params.items() if v is not None})
        except httpx.HTTPError as e:
            raise OpusError(f"OPUS Library unreachable at {self._cfg.base_url}: {e}") from e
        return self._unwrap(r, path)

    async def _get_bytes(self, path: str, **params: Any) -> bytes:
        try:
            r = await self._http.get(path, params=params, headers={"Accept": "image/*"})
        except httpx.HTTPError as e:
            raise OpusError(f"OPUS Library unreachable at {self._cfg.base_url}: {e}") from e
        if r.status_code >= 400:
            raise OpusError(f"OPUS GET {path} -> HTTP {r.status_code}: {r.text[:200]}")
        return r.content

    def _unwrap(self, r: httpx.Response, path: str) -> Any:
        if r.status_code == 401:
            raise OpusError(
                "OPUS Library rejected the token (HTTP 401). Use the library's "
                "service token (Settings → Access → service token)."
            )
        if r.status_code >= 400:
            raise OpusError(f"OPUS GET {path} -> HTTP {r.status_code}: {r.text[:200]}")
        try:
            return r.json()
        except ValueError as e:
            raise OpusError(f"OPUS GET {path} returned non-JSON") from e

    async def ping(self) -> int:
        """Prove both the address and the token. `/api/ping` answers without
        a token, so on its own it would pass a wrong one; the people list is
        the first read that actually needs the credential. Returns how many
        named people the library holds."""
        ok = await self._get("/api/ping")
        if not isinstance(ok, dict) or not ok.get("ok"):
            raise OpusError("OPUS Library did not answer /api/ping as itself")
        return len(await self.list_people())

    async def list_people(self) -> list[OpusPerson]:
        data = await self._get("/api/photos/people")
        if not isinstance(data, list):
            raise OpusError("OPUS /api/photos/people: unexpected payload")
        people: list[OpusPerson] = []
        for p in data:
            try:
                years = p.get("years") or [None, None]
                people.append(
                    OpusPerson(
                        id=int(p["id"]),
                        name=str(p["name"]),
                        given_name=str(p.get("given_name") or ""),
                        faces=int(p.get("faces") or 0),
                        first_year=int(years[0]) if years[0] is not None else None,
                        last_year=int(years[1]) if years[1] is not None else None,
                        cover_face_id=int(p["cover"]) if p.get("cover") is not None else None,
                    )
                )
            except (KeyError, TypeError, ValueError):
                log.warning("opus: skipping malformed person row %r", p)
        return people

    async def get_person(self, person_id: int) -> OpusPerson | None:
        return next((p for p in await self.list_people() if p.id == person_id), None)

    async def faces_for_person(
        self,
        person_id: int,
        *,
        since: _dt.date | None,
        min_px: int,
        per_day: int,
        limit: int,
    ) -> tuple[str, list[OpusFace]]:
        """Newest first, largest first within a moment, at most `per_day`
        faces from any one day of photographs. Returns the name the library
        has for the person alongside the faces, so a caller that only held an
        id has a word for the log line and the response."""
        data = await self._get(
            f"/api/photos/people/{person_id}/faces",
            limit=limit,
            since=since.isoformat() if since else None,
            min_px=min_px,
            per_day=per_day,
        )
        if not isinstance(data, dict) or "faces" not in data:
            raise OpusError(f"OPUS person {person_id}: unexpected payload")
        faces: list[OpusFace] = []
        for f in data["faces"]:
            try:
                taken = f.get("taken_at")
                faces.append(
                    OpusFace(
                        id=int(f["id"]),
                        photo=str(f["photo"]),
                        taken_at=_dt.datetime.fromisoformat(taken) if taken else None,
                        x=float(f["x"]),
                        y=float(f["y"]),
                        w=float(f["w"]),
                        h=float(f["h"]),
                        px=int(f.get("px") or 0),
                        score=float(f.get("score") or 0.0),
                    )
                )
            except (KeyError, TypeError, ValueError):
                log.warning("opus: skipping malformed face row %r", f)
        return str(data.get("person", {}).get("name") or ""), faces

    async def household_person_ids(self) -> set[int]:
        """Which library people also hold an account — the household, as
        against everyone else who was ever photographed."""
        data = await self._get("/api/auth/people")
        people = data.get("people") if isinstance(data, dict) else None
        if not isinstance(people, list):
            raise OpusError("OPUS /api/auth/people: unexpected payload")
        return {int(p["person_id"]) for p in people if p.get("person_id") is not None}

    async def download_preview(self, checksum: str) -> bytes:
        """The 2048 px preview of a photograph. AVIF — the api's Pillow
        decodes it (checked in the production image before this was written)."""
        return await self._get_bytes(f"/api/photos/{checksum}/preview")

    async def download_face_crop(self, face_id: int) -> bytes:
        """The library's own thumbnail of a face, for the picker. JPEG."""
        return await self._get_bytes(f"/api/photos/faces/{face_id}/crop", size="face")


async def load_config(pool: asyncpg.Pool, secret_key: str) -> OpusConfig | None:
    row = await pool.fetchrow("SELECT value FROM app_settings WHERE key = $1", OPUS_SETTINGS_KEY)
    if row is None:
        return None
    value = row["value"]
    if isinstance(value, str):
        value = json.loads(value)
    return OpusConfig.from_row(value, secret_key)
