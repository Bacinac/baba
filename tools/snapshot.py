"""Capture one frame + matching detections and save an annotated JPG.

Subscribes to baba.frames.* and baba.detections.* for a short window, pairs
them by (camera_id, sequence), draws bounding boxes, writes to /out.

Usage (inside a container on the baba_default network):
    python /tools/snapshot.py --camera cam01 --out /out/snapshot.jpg --conf 0.5
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import cv2
import msgspec
import nats
import numpy as np

log = logging.getLogger("baba.snapshot")


class FrameMessage(msgspec.Struct, frozen=True):
    camera_id: str
    sequence: int
    timestamp_ns: int
    width: int
    height: int
    channels: int
    dtype: str
    pixels: bytes


class DetectionWire(msgspec.Struct, frozen=True):
    x1: float
    y1: float
    x2: float
    y2: float
    class_id: int
    class_name: str
    confidence: float


class DetectionsMessage(msgspec.Struct, frozen=True):
    camera_id: str
    sequence: int
    timestamp_ns: int
    detections: list[DetectionWire]


PALETTE = [
    (255, 56, 56), (255, 159, 56), (255, 213, 56), (171, 255, 56),
    (56, 255, 122), (56, 255, 232), (56, 173, 255), (88, 56, 255),
    (197, 56, 255), (255, 56, 191),
]


def draw(image_rgb: np.ndarray, dets: list[DetectionWire], conf: float) -> np.ndarray:
    bgr = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2BGR)
    for d in dets:
        if d.confidence < conf:
            continue
        color = PALETTE[d.class_id % len(PALETTE)]
        x1, y1, x2, y2 = int(d.x1), int(d.y1), int(d.x2), int(d.y2)
        cv2.rectangle(bgr, (x1, y1), (x2, y2), color, thickness=3)
        label = f"{d.class_name} {d.confidence:.2f}"
        (tw, th), bl = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.8, 2)
        cv2.rectangle(bgr, (x1, y1 - th - bl - 4), (x1 + tw + 4, y1), color, -1)
        cv2.putText(
            bgr, label, (x1 + 2, y1 - bl - 2),
            cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 2, cv2.LINE_AA,
        )
    return bgr


async def main_async(args: argparse.Namespace) -> int:
    frame_dec = msgspec.msgpack.Decoder(FrameMessage)
    det_dec = msgspec.msgpack.Decoder(DetectionsMessage)

    nc = await nats.connect(args.nats_url, name="baba-snapshot")
    log.info("connected to %s", args.nats_url)

    frames: dict[tuple[str, int], FrameMessage] = {}
    pending: list[DetectionsMessage] = []
    done = asyncio.Event()
    result: dict[str, object] = {}

    async def on_frame(msg) -> None:
        m = frame_dec.decode(msg.data)
        if m.camera_id != args.camera:
            return
        frames[(m.camera_id, m.sequence)] = m
        # Try to match against any pending detections
        for d in list(pending):
            if d.camera_id == m.camera_id and d.sequence == m.sequence:
                result["frame"] = m
                result["dets"] = d
                done.set()
                pending.remove(d)
                return

    async def on_det(msg) -> None:
        d = det_dec.decode(msg.data)
        if d.camera_id != args.camera:
            return
        key = (d.camera_id, d.sequence)
        if key in frames:
            result["frame"] = frames[key]
            result["dets"] = d
            done.set()
        else:
            pending.append(d)

    await nc.subscribe(f"baba.frames.{args.camera}", cb=on_frame)
    await nc.subscribe(f"baba.detections.{args.camera}", cb=on_det)

    try:
        await asyncio.wait_for(done.wait(), timeout=args.timeout)
    except TimeoutError:
        log.error("timeout — no matched frame+detections within %ds", args.timeout)
        return 2

    frame: FrameMessage = result["frame"]  # type: ignore[assignment]
    dets: DetectionsMessage = result["dets"]  # type: ignore[assignment]
    pixels = np.frombuffer(frame.pixels, dtype=frame.dtype).reshape(
        frame.height, frame.width, frame.channels
    )
    log.info(
        "matched seq=%d  frame=%dx%d  detections=%d",
        frame.sequence, frame.width, frame.height, len(dets.detections),
    )
    annotated = draw(pixels, dets.detections, args.conf)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(args.out), annotated, [cv2.IMWRITE_JPEG_QUALITY, 90])
    above = sum(1 for d in dets.detections if d.confidence >= args.conf)
    log.info("wrote %s — %d boxes >=%.2f conf", args.out, above, args.conf)
    await nc.drain()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nats-url", default="nats://nats:4222")
    parser.add_argument("--camera", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--conf", type=float, default=0.5)
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
