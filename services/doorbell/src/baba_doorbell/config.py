from __future__ import annotations

import os
from dataclasses import dataclass

from baba_core.dsn import dsn_from_env


@dataclass(slots=True, frozen=True)
class DoorbellConfig:
    dsn: str
    # Camera slugs that are doorbells (their `cameras.stream_url` carries the
    # host + credentials we reach over the LAN). Comma-separated env; defaults
    # to no cameras when this installation has no doorbell.
    slugs: tuple[str, ...]
    # Backoff before reconnecting a dropped Baichuan session.
    reconnect_seconds: float
    # Poll fallback cadence. Baichuan push is the primary, instant path; this
    # get_states() edge-check is a backstop for a missed push (and doubles as
    # the connection-liveness probe). Kept short so a ring is never lost even
    # if push wiring misbehaves on a given firmware.
    poll_seconds: float

    @classmethod
    def from_env(cls) -> DoorbellConfig:
        slugs = tuple(
            s.strip()
            for s in os.environ.get("BABA_DOORBELL_SLUGS", "").split(",")
            if s.strip()
        )
        return cls(
            dsn=dsn_from_env(),
            slugs=slugs,
            reconnect_seconds=float(os.environ.get("BABA_DOORBELL_RECONNECT_SECONDS", "10")),
            poll_seconds=float(os.environ.get("BABA_DOORBELL_POLL_SECONDS", "5")),
        )
