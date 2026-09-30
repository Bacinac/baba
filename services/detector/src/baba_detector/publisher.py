from __future__ import annotations

import msgspec
from baba_core import Detection
from baba_core.wire import (
    SUBJECT_DETECTIONS_TEMPLATE,
    DetectionsMessage,
    DetectionWire,
)
from nats.aio.client import Client as NATS


class DetectionPublisher:
    def __init__(self, nc: NATS) -> None:
        self._nc = nc
        self._encoder = msgspec.msgpack.Encoder()

    async def publish(
        self,
        camera_id: str,
        sequence: int,
        timestamp_ns: int,
        frame_width: int,
        frame_height: int,
        detections: list[Detection],
        birth_eligible: list[bool] | None = None,
        pts_ns: int = 0,
    ) -> None:
        # birth_eligible parallels detections (True when the score cleared the
        # per-camera birth threshold). Absent → all True (birth-capable), so a
        # caller that hasn't adopted two-threshold behaves as before.
        flags = birth_eligible if birth_eligible is not None else [True] * len(detections)
        msg = DetectionsMessage(
            camera_id=camera_id,
            sequence=sequence,
            timestamp_ns=timestamp_ns,
            frame_width=frame_width,
            frame_height=frame_height,
            pts_ns=pts_ns,
            detections=[
                DetectionWire(
                    x1=d.bbox.x1,
                    y1=d.bbox.y1,
                    x2=d.bbox.x2,
                    y2=d.bbox.y2,
                    class_id=d.class_id,
                    class_name=d.class_name,
                    confidence=d.confidence,
                    birth_eligible=flags[i],
                )
                for i, d in enumerate(detections)
            ],
        )
        await self._nc.publish(
            SUBJECT_DETECTIONS_TEMPLATE.format(camera_id=camera_id),
            self._encoder.encode(msg),
        )
