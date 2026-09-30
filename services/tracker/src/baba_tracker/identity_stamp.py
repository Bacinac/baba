"""Identity at the source — name PET tracks inside the tracker.

The tracker already computes an OSNet embedding for every detection (ReID),
and enrolled reference photos (`identity_reference_photos.body_embedding`)
live in the SAME OSNet space — so a pet can be named within ~a second of
entering the frame, and the name rides the TrackWire for the track's whole
life (anchor holds included). Downstream consumers (event-manager snapshot,
DIDA badges, rules) read it off the wire instead of each running their own
DB kNN.

Matching is the exact rule the event-manager's finalize body-reference match
uses: MIN over the identity's reference photos, restricted to identities of
the pet kind (dog/cat pooled — the detector flips between them), adopt the
nearest identity ONLY when under the threshold AND clearly closer than the
runner-up by the margin. A match is STICKY for the track's life — pets have
no face stack, so nothing can override it, and flapping names would churn
every consumer.

PERSONS ARE NEVER STAMPED — naming people by body appearance is the
over-merge vortex. VEHICLES ARE NEVER STAMPED EITHER: measured on this
property, a stranger's dark car sits CLOSER to the enrolled reference than
the owner's own car (0.128 against 0.170), so appearance cannot name a
vehicle here — the plate does, downstream, or nothing does.

References reload every `_REFRESH_S` from Postgres (they change rarely — an
enrollment is an operator action); a DB outage keeps the last good set and
logs, it never drops names mid-track.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import asyncpg
import msgspec.structs
import numpy as np
from baba_core import PET_GROUP
from baba_core.pipeline_settings import Settings
from baba_core.wire import TrackWire

log = logging.getLogger("baba.tracker.identity")

_REFRESH_S = 300
# Re-attempt an unresolved track's match at most this often — embeddings on a
# live track barely change between consecutive ticks.
_RETRY_NS = 2_000_000_000


def _kind_for_class(class_id: int) -> str | None:
    if class_id in PET_GROUP:
        return "pet"
    return None  # persons, vehicles and everything else: never body-named


def _parse_vec(raw: object) -> np.ndarray | None:
    """asyncpg returns pgvector as its text form '[0.1,0.2,…]' (no codec
    registered). Parse + L2-normalise."""
    if raw is None:
        return None
    try:
        s = raw if isinstance(raw, str) else str(raw)
        v = np.array([float(x) for x in s.strip("[]").split(",")], dtype=np.float32)
    except ValueError:
        return None
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else None


@dataclass(slots=True)
class _Ref:
    gid: str
    name: str
    kind: str
    embs: list[np.ndarray]


@dataclass(slots=True)
class _Sticky:
    gid: str = ""
    name: str = ""
    last_try_ns: int = 0
    last_seen_ns: int = 0


class IdentityStamp:
    """Loads pet/vehicle reference embeddings and stamps identity onto
    emitted non-person TrackWires. One instance per process."""

    def __init__(self, dsn: str, settings: Settings, *, available: bool) -> None:
        self._dsn = dsn
        # Read live — see AnchorHold for why these are not copied here.
        self._s = settings
        # References live in OSNet space, so without ReID this is off whatever
        # the operator sets.
        self._available = available
        self._refs: list[_Ref] = []
        # (camera_slug, track_id) → sticky verdict / retry throttle
        self._sticky: dict[tuple[str, int], _Sticky] = {}
        self._last_gc = 0.0

    @property
    def _pet_threshold(self) -> float:
        return self._s.f("identity_pet_threshold") if self._available else 0.0

    @property
    def _margin(self) -> float:
        return self._s.f("identity_margin")

    @property
    def enabled(self) -> bool:
        return self._pet_threshold > 0

    async def refresh_loop(self) -> None:
        """Reload references every _REFRESH_S. Keeps the last good set on DB
        errors — a naming outage must not un-name live tracks."""
        while True:
            try:
                await self._refresh_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("identity reference reload failed; keeping previous set")
            await asyncio.sleep(_REFRESH_S)

    async def _refresh_once(self) -> None:
        conn = await asyncpg.connect(self._dsn)
        try:
            rows = await conn.fetch(
                """
                SELECT il.global_id::text AS gid, il.name,
                       COALESCE(il.match_kind, il.kind) AS kind,
                       rp.body_embedding::text AS emb
                FROM identity_reference_photos rp
                JOIN identity_labels il ON il.global_id = rp.global_id
                WHERE rp.body_embedding IS NOT NULL
                  AND COALESCE(il.match_kind, il.kind) = 'pet'
                  AND il.name IS NOT NULL
                """
            )
        finally:
            await conn.close()
        by_gid: dict[str, _Ref] = {}
        for r in rows:
            v = _parse_vec(r["emb"])
            if v is None:
                continue
            ref = by_gid.get(r["gid"])
            if ref is None:
                ref = _Ref(gid=r["gid"], name=r["name"], kind=r["kind"], embs=[])
                by_gid[r["gid"]] = ref
            ref.embs.append(v)
        prev = sum(len(r.embs) for r in self._refs)
        self._refs = list(by_gid.values())
        cur = sum(len(r.embs) for r in self._refs)
        if cur != prev:
            log.info(
                "identity references loaded: %d identit(ies), %d photo embedding(s)",
                len(self._refs), cur,
            )

    def _match(self, kind: str, emb: np.ndarray) -> tuple[str, str] | None:
        thr = self._pet_threshold
        if thr <= 0:
            return None
        best: tuple[float, _Ref] | None = None
        runner: float | None = None
        for ref in self._refs:
            if ref.kind != kind:
                continue
            d = min(float(1.0 - np.dot(e, emb)) for e in ref.embs)
            if best is None or d < best[0]:
                runner = best[0] if best is not None else None
                best = (d, ref)
            elif runner is None or d < runner:
                runner = d
        if best is None or best[0] >= thr:
            return None
        if runner is not None and (runner - best[0]) < self._margin:
            return None  # ambiguous — two comparably-close identities
        return best[1].gid, best[1].name

    def stamp(
        self,
        cam: str,
        wires: list[TrackWire],
        emb_by_tid: dict[int, np.ndarray],
        ts_ns: int,
    ) -> list[TrackWire]:
        """Return wires with identity fields filled for matched non-persons.
        Sticky per (camera, track id); unresolved tracks retry ~every 2 s."""
        if not self.enabled or not self._refs:
            return wires
        out: list[TrackWire] = []
        for w in wires:
            kind = _kind_for_class(w.class_id)
            if kind is None:
                out.append(w)
                continue
            key = (cam, w.track_id)
            st = self._sticky.get(key)
            if st is None:
                st = _Sticky()
                self._sticky[key] = st
            st.last_seen_ns = ts_ns
            if not st.gid and ts_ns - st.last_try_ns >= _RETRY_NS:
                st.last_try_ns = ts_ns
                emb = emb_by_tid.get(w.track_id)
                if emb is not None:
                    m = self._match(kind, emb)
                    if m is not None:
                        st.gid, st.name = m
                        log.info(
                            "identity stamped cam=%s track=%s -> %r (%s)",
                            cam, w.track_id, st.name, kind,
                        )
            if st.gid:
                out.append(
                    msgspec.structs.replace(
                        w, identity_gid=st.gid, identity_name=st.name
                    )
                )
            else:
                out.append(w)
        self._gc(ts_ns)
        return out

    def _gc(self, ts_ns: int) -> None:
        now = time.monotonic()
        if now - self._last_gc < 60.0:
            return
        self._last_gc = now
        cutoff = ts_ns - 300 * 1_000_000_000
        stale = [k for k, s in self._sticky.items() if s.last_seen_ns < cutoff]
        for k in stale:
            del self._sticky[k]

    def forget_camera(self, cam: str) -> None:
        stale = [k for k in self._sticky if k[0] == cam]
        for k in stale:
            del self._sticky[k]


__all__ = ["IdentityStamp"]
