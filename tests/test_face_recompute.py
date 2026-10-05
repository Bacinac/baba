"""Reference recompute: every photo's face is re-embedded under the loaded
stack, and a photo that already is an aligned face is embedded as one.

A from-face-samples reference stores the aligned 112x112 face its vector came
from. The detector finds nothing inside most of those, so asking it would
erase the reference; the embedder takes them directly.
"""

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import asyncpg
import cv2
import numpy as np
import pytest
from baba_api import routes_face_recognition as fr

DIM = 512


def unit(axis: int) -> np.ndarray:
    v = np.zeros(DIM, dtype=np.float32)
    v[axis] = 1.0
    return v


def literal(v: np.ndarray) -> str:
    return "[" + ",".join(map(repr, v.tolist())) + "]"


class Stack:
    """Detects a face only in photos wider than 112 px, and embeds by shade."""

    def __init__(self) -> None:
        self.embedder = SimpleNamespace(embed=self.embed)
        self.detected: list[tuple[int, int]] = []

    @staticmethod
    def embed(aligned: np.ndarray) -> np.ndarray:
        assert aligned.shape[:2] == (112, 112)
        return unit(int(aligned[0, 0, 0]) % DIM)

    def embed_from_crop(self, rgb: np.ndarray):
        self.detected.append(rgb.shape[:2])
        if rgb.shape[1] <= 112:
            return None
        return unit(100), (20.0, 10.0, 80.0, 90.0), 0.9


def write(tmp_path, name: str, shape: tuple[int, int], shade: int) -> str:
    rel = f"reference_photos/{name}.png"
    (tmp_path / "reference_photos").mkdir(exist_ok=True)
    cv2.imwrite(str(tmp_path / rel), np.full((*shape, 3), shade, dtype=np.uint8))
    return rel


def test_recompute_embeds_aligned_faces_directly_and_detects_the_rest(pg, tmp_path, monkeypatch):
    stack = Stack()
    monkeypatch.setenv("BABA_MEDIA_PATH", str(tmp_path))
    monkeypatch.setattr(fr, "make_face_stack_for_model", lambda **_: (stack, "scrfd_10g", "m2"))

    async def main():
        pool = await asyncpg.create_pool(pg, min_size=1, max_size=2)
        try:
            gid = uuid4()
            async with pool.acquire() as conn:
                await conn.execute("UPDATE face_recognition_settings SET model_key='m2', detector_key='scrfd_10g'")
                await conn.execute(
                    "INSERT INTO identity_labels (global_id, name, kind) VALUES ($1, 'x', 'person')",
                    gid,
                )
                ids = {}
                for name, shape, source, px, bbox in (
                    ("sample", (112, 112), "from-face-samples", 74.0, [0.2, 0.0, 0.9, 1.0]),
                    ("portrait", (160, 120), "opus", 55.0, [0.0, 0.0, 1.0, 1.0]),
                    ("faceless", (112, 100), "upload", 50.0, [0.1, 0.1, 0.5, 0.5]),
                    ("gone", (112, 112), "upload", 50.0, None),
                ):
                    rel = write(tmp_path, name, shape, 7)
                    ids[name] = await conn.fetchval(
                        """
                        INSERT INTO identity_reference_photos
                            (global_id, photo_path, source, face_embedding, face_embedding_model, face_px, face_bbox)
                        VALUES ($1, $2, $3, $4::text::vector, 'm1', $5, $6)
                        RETURNING id
                        """,
                        gid,
                        rel,
                        source,
                        literal(unit(1)),
                        px,
                        bbox,
                    )
                (tmp_path / "reference_photos/gone.png").unlink()
                job = await conn.fetchval(
                    "INSERT INTO face_recompute_jobs (model_key) VALUES ('m2') RETURNING id"
                )
            await fr._run_recompute(pool, job, "m2", "scrfd_10g")
            async with pool.acquire() as conn:
                j = await conn.fetchrow("SELECT * FROM face_recompute_jobs WHERE id = $1", job)
                rows = {
                    name: await conn.fetchrow(
                        "SELECT face_embedding::text AS vec, face_embedding_model AS model, face_px, face_bbox "
                        "FROM identity_reference_photos WHERE id = $1",
                        rid,
                    )
                    for name, rid in ids.items()
                }
        finally:
            await pool.close()
        return j, rows

    j, rows = asyncio.run(main())
    assert (j["status"], j["total"], j["succeeded"], j["no_face"], j["missing_file"]) == (
        "done",
        4,
        2,
        1,
        1,
    )
    assert stack.detected == [(160, 120), (112, 100)]

    sample = rows["sample"]
    assert (sample["model"], sample["face_px"]) == ("m2", 74.0)
    assert sample["face_bbox"] == [0.0, 0.0, 1.0, 1.0]
    assert json.loads(sample["vec"])[7] == 1.0

    portrait = rows["portrait"]
    assert (portrait["model"], portrait["face_px"]) == ("m2", 60.0)
    assert portrait["face_bbox"] == pytest.approx([20 / 120, 10 / 160, 80 / 120, 90 / 160])

    faceless = rows["faceless"]
    assert (faceless["vec"], faceless["model"], faceless["face_px"], faceless["face_bbox"]) == (
        None,
        "m2",
        None,
        None,
    )

    assert rows["gone"]["model"] == "m1"
