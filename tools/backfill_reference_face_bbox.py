"""Fill face_bbox / face_px on reference photos enrolled before 069.

Run inside the api container:

    docker compose exec -T api python /app/tools/backfill_reference_face_bbox.py

Deliberately NOT the /face-recognition/recompute job. That job re-detects
every photo and writes NULL when detection fails — and photos enrolled from
the CCTV face path ARE aligned 112x112 faces, on which SCRFD frequently finds
nothing, because there is no surrounding image left to find a face in. Running
recompute for the sake of a bounding box would therefore delete the very face
references it was meant to annotate.

So this only ever ADDS: it touches rows that already carry a face vector and
lack a box, and it never clears an embedding. Where detection fails, the photo
is taken to BE the face (that is the only way it got a vector without a
detectable face in it) and the box becomes the whole frame.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import asyncpg
import cv2
from baba_core import make_face_stack_for_model

MEDIA = Path(os.environ.get("BABA_MEDIA_PATH", "/media"))
MODELS = Path(os.environ.get("BABA_MODELS_PATH", "/models"))


async def main() -> None:
    pool = await asyncpg.create_pool(
        host=os.environ["POSTGRES_HOST"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        database=os.environ["POSTGRES_DB"],
        min_size=1,
        max_size=2,
    )
    settings = await pool.fetchrow(
        "SELECT model_key, detector_key FROM face_recognition_settings LIMIT 1"
    )
    stack, _det, active = await asyncio.to_thread(
        make_face_stack_for_model,
        yunet_path=Path(
            os.environ.get("BABA_FACE_DETECTOR_MODEL", str(MODELS / "face_yunet.onnx"))
        ),
        models_dir=MODELS,
        model_key=settings["model_key"],
        detector_key=settings["detector_key"],
    )
    if stack is None:
        raise SystemExit("face stack failed to load")
    print(f"face stack: {active}")

    rows = await pool.fetch(
        "SELECT id, photo_path FROM identity_reference_photos "
        "WHERE face_embedding IS NOT NULL AND face_bbox IS NULL"
    )
    print(f"{len(rows)} reference photos to annotate")

    n_detected = n_whole = n_missing = 0
    for r in rows:
        path = MEDIA / r["photo_path"]
        img = cv2.imread(str(path))
        if img is None:
            n_missing += 1
            continue
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        res = await asyncio.to_thread(stack.embed_from_crop, rgb)
        h, w = rgb.shape[0], rgb.shape[1]
        if res is None:
            bbox = [0.0, 0.0, 1.0, 1.0]
            px = float(min(w, h))
            n_whole += 1
        else:
            x1, y1, x2, y2 = res[1]
            bbox = [
                max(0.0, min(1.0, x1 / w)),
                max(0.0, min(1.0, y1 / h)),
                max(0.0, min(1.0, x2 / w)),
                max(0.0, min(1.0, y2 / h)),
            ]
            px = float(min(x2 - x1, y2 - y1))
            n_detected += 1
        await pool.execute(
            "UPDATE identity_reference_photos "
            "SET face_bbox = $1, face_px = COALESCE(face_px, $2) WHERE id = $3",
            bbox,
            px,
            r["id"],
        )
    print(f"detected={n_detected} whole-photo={n_whole} unreadable={n_missing}")
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
