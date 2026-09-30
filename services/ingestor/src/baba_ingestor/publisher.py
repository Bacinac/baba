from __future__ import annotations

import logging

import msgspec
import numpy as np
from baba_core import mask_credentials
from baba_core.nats_conn import connect as nats_connect
from baba_core.nats_conn import drain_quietly
from baba_core.wire import SUBJECT_FRAMES_TEMPLATE, FrameMessage
from nats.aio.client import Client as NATS

log = logging.getLogger(__name__)


class NATSFramePublisher:
    def __init__(self, nats_url: str, camera_id: str) -> None:
        self._nats_url = nats_url
        self._camera_id = camera_id
        self._subject = SUBJECT_FRAMES_TEMPLATE.format(camera_id=camera_id)
        self._nc: NATS | None = None
        self._encoder = msgspec.msgpack.Encoder()

    async def connect(self) -> None:
        self._nc = await nats_connect(self._nats_url, name=f"ingestor-{self._camera_id}")
        log.info("connected to NATS: %s", mask_credentials(self._nats_url))

    async def publish(
        self,
        sequence: int,
        timestamp_ns: int,
        pixels: np.ndarray,
        pts_ns: int = 0,
    ) -> None:
        """Notify subscribers that frame `sequence` is live in shm. `pixels`
        is taken only for its shape — the buffer itself stays in the ring."""
        if self._nc is None:
            raise RuntimeError("not connected")
        msg = FrameMessage(
            camera_id=self._camera_id,
            sequence=sequence,
            timestamp_ns=timestamp_ns,
            height=pixels.shape[0],
            width=pixels.shape[1],
            pts_ns=pts_ns,
        )
        await self._nc.publish(self._subject, self._encoder.encode(msg))

    async def close(self) -> None:
        if self._nc is not None:
            await drain_quietly(self._nc)
            self._nc = None
