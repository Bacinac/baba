"""Reference-photo enrolment: which candidates become rows, and why the rest
are refused.

A person's body reference needs a usable face in the same photo; a pet's does
not. A face below the size floor never reaches the database, and a
near-duplicate of an enrolled reference or of an earlier candidate is dropped
before anything is written.
"""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import asyncpg
import numpy as np
import pytest
from baba_api.routes_identities._reference_photos import _persist_reference_photos
from fastapi import HTTPException

DIM = 512


def unit(axis: int) -> np.ndarray:
    v = np.zeros(DIM, dtype=np.float32)
    v[axis] = 1.0
    return v


def literal(v: np.ndarray) -> str:
    return "[" + ",".join(map(repr, v.tolist())) + "]"


def crop(shade: int) -> np.ndarray:
    return np.full((200, 100, 3), shade, dtype=np.uint8)


def box(side: float) -> tuple[float, float, float, float]:
    return (10.0, 10.0, 10.0 + side, 10.0 + side)


class Bodies:
    def __init__(self, *vectors: np.ndarray) -> None:
        self.vectors = list(vectors)

    def embed(self, crops):
        assert len(crops) == len(self.vectors)
        return self.vectors


class Faces:
    """Answers per crop, keyed by the crop's fill value."""

    def __init__(self, by_shade: dict[int, tuple[np.ndarray, tuple] | None]) -> None:
        self.by_shade = by_shade

    def embed_from_crop(self, c: np.ndarray):
        return self.by_shade[int(c[0, 0, 0])]


def request(pool, tmp_path, *, bodies=None, faces=None, face_error=None):
    state = SimpleNamespace(
        pool=pool,
        embedder_backend=bodies,
        face_stack=faces,
        face_stack_error=face_error,
        face_model_key="m1",
        config=SimpleNamespace(media_path=tmp_path),
    )
    return SimpleNamespace(app=SimpleNamespace(state=state))


USER = SimpleNamespace(id=None, username="test")


async def identity(conn, kind: str) -> object:
    gid = uuid4()
    await conn.execute(
        "INSERT INTO identity_labels (global_id, name, kind) VALUES ($1, $2, $3)",
        gid, str(gid), kind,
    )
    return gid


async def rows(conn, gid):
    return await conn.fetch(
        """
        SELECT body_embedding IS NOT NULL AS body, face_embedding IS NOT NULL AS face,
               face_embedding_model, face_px, face_bbox, origin_ref, source, photo_path
        FROM identity_reference_photos WHERE global_id = $1 ORDER BY face_px NULLS LAST
        """,
        gid,
    )


def run(pg, body):
    async def main():
        pool = await asyncpg.create_pool(pg, min_size=1, max_size=2)
        try:
            await body(pool)
        finally:
            await pool.close()

    asyncio.run(main())


def test_a_persons_body_reference_needs_a_usable_face(pg, tmp_path):
    async def body(pool):
        async with pool.acquire() as conn:
            gid = await identity(conn, "person")
        req = request(
            pool,
            tmp_path,
            bodies=Bodies(unit(0), unit(1), unit(2), unit(2)),
            faces=Faces({1: (unit(10), box(60)), 2: (unit(11), box(20)), 3: None, 4: None}),
        )
        result = await _persist_reference_photos(
            req, gid, USER, [crop(1), crop(2), crop(3), crop(4)], ["bad.jpg: unreadable"], "upload"
        )
        assert result["used"] == 1
        assert result["faces_detected"] == 1
        assert result["skipped"] == [
            "bad.jpg: unreadable",
            "crop 4: near-duplicate of already-selected crop 3 (identical)",
            "crop 2: no usable face in this photo, so it cannot be trusted as a body reference for a person",
            "crop 3: no usable face in this photo, so it cannot be trusted as a body reference for a person",
        ]
        assert result["label"]["reference_count"] == 1
        async with pool.acquire() as conn:
            (row,) = await rows(conn, gid)
        assert (row["body"], row["face"], row["face_embedding_model"], row["face_px"]) == (True, True, "m1", 60.0)
        assert row["face_bbox"] == pytest.approx([0.1, 0.05, 0.7, 0.35])
        assert row["source"] == "upload"
        assert (tmp_path / row["photo_path"]).is_file()

    run(pg, body)


def test_a_pets_body_reference_stands_without_a_face(pg, tmp_path):
    async def body(pool):
        async with pool.acquire() as conn:
            gid = await identity(conn, "pet")
            await conn.execute(
                """
                INSERT INTO identity_reference_photos (global_id, photo_path, body_embedding)
                VALUES ($1, 'reference_photos/old.jpg', $2::text::vector)
                """,
                gid, literal(unit(5)),
            )
        req = request(
            pool,
            tmp_path,
            bodies=Bodies(unit(5), unit(6), unit(7)),
            faces=Faces({1: None, 2: (unit(12), box(30)), 3: None}),
        )
        result = await _persist_reference_photos(
            req, gid, USER, [crop(1), crop(2), crop(3)], [], "from_tracks", candidate_labels=["a", "b", "c"]
        )
        assert (result["used"], result["faces_detected"]) == (2, 0)
        assert result["skipped"] == ["a: near-duplicate of existing reference (identical)"]
        async with pool.acquire() as conn:
            got = await rows(conn, gid)
        assert [(r["body"], r["face"], r["face_px"], r["face_bbox"]) for r in got] == [(True, False, None, None)] * 3

    run(pg, body)


def test_a_face_only_import_keeps_faces_and_stores_no_body(pg, tmp_path):
    async def body(pool):
        async with pool.acquire() as conn:
            gid = await identity(conn, "person")
            await conn.execute(
                """
                INSERT INTO identity_reference_photos (global_id, photo_path, face_embedding)
                VALUES ($1, 'reference_photos/old.jpg', $2::text::vector)
                """,
                gid, literal(unit(23)),
            )
        req = request(pool, tmp_path, faces=Faces({}))
        result = await _persist_reference_photos(
            req,
            gid,
            USER,
            [crop(1), crop(2), crop(3), crop(4), crop(5)],
            [],
            "opus",
            pre_face_embeddings=[unit(20), unit(21), None, unit(23), unit(20)],
            pre_face_px=[80.0, 30.0, None, 90.0, 85.0],
            pre_face_model_key="m1",
            face_only=True,
            origin_refs=["face:1", "face:2", "face:3", "face:4", "face:5"],
        )
        assert (result["used"], result["faces_detected"]) == (1, 1)
        assert result["skipped"] == [
            "crop 3: no face found by BABA's detector",
            "crop 2: face too small to identify (30 px, need 40)",
            "crop 4: near-duplicate of existing reference (identical)",
            "crop 5: near-duplicate of already-selected crop 1 (identical)",
        ]
        async with pool.acquire() as conn:
            got = await rows(conn, gid)
        new = [r for r in got if r["origin_ref"] is not None]
        assert [(r["body"], r["face"], r["face_px"], r["face_bbox"], r["origin_ref"]) for r in new] == [
            (False, True, 80.0, [0.0, 0.0, 1.0, 1.0], "face:1")
        ]

    run(pg, body)


def test_a_face_only_import_detects_faces_itself_without_vectors(pg, tmp_path):
    async def body(pool):
        async with pool.acquire() as conn:
            gid = await identity(conn, "person")
        req = request(pool, tmp_path, faces=Faces({1: (unit(30), box(50)), 2: None}))
        result = await _persist_reference_photos(req, gid, USER, [crop(1), crop(2)], [], "immich", face_only=True)
        assert (result["used"], result["faces_detected"]) == (1, 1)
        assert result["skipped"] == ["crop 2: no face found by BABA's detector"]
        async with pool.acquire() as conn:
            (row,) = await rows(conn, gid)
        assert row["face_px"] == 50.0
        assert row["face_bbox"] == pytest.approx([0.1, 0.05, 0.6, 0.3])

    run(pg, body)


def test_nothing_kept_returns_the_label_untouched(pg, tmp_path):
    async def body(pool):
        async with pool.acquire() as conn:
            gid = await identity(conn, "person")
        req = request(pool, tmp_path, bodies=Bodies(unit(0)), faces=Faces({1: None}))
        result = await _persist_reference_photos(req, gid, USER, [crop(1)], [], "upload")
        assert (result["used"], result["faces_detected"]) == (0, 0)
        assert result["label"]["global_id"] == str(gid)
        async with pool.acquire() as conn:
            assert await rows(conn, gid) == []

    run(pg, body)


def test_an_unknown_identity_gets_a_label_row(pg, tmp_path):
    async def body(pool):
        gid = uuid4()
        req = request(pool, tmp_path, bodies=Bodies(unit(0)))
        result = await _persist_reference_photos(req, gid, USER, [crop(1)], [], "upload")
        assert result["used"] == 1
        assert result["label"]["name"] == f"Identitet {str(gid)[:8]}"

    run(pg, body)


@pytest.mark.parametrize(
    ("kwargs", "face_only", "fragment"),
    [
        ({"faces": Faces({}), "face_error": "cuda gone"}, False, "cuda gone"),
        ({}, True, "face-only import has nothing"),
        ({"bodies": Bodies(unit(0))}, False, "face stack unavailable"),
        ({"faces": Faces({})}, False, "BABA_EMBEDDER_MODEL"),
    ],
)
def test_enrolment_refuses_loudly_when_a_model_is_missing(pg, tmp_path, kwargs, face_only, fragment):
    async def body(pool):
        async with pool.acquire() as conn:
            gid = await identity(conn, "person")
        req = request(pool, tmp_path, **kwargs)
        with pytest.raises(HTTPException) as e:
            await _persist_reference_photos(req, gid, USER, [crop(1)], [], "upload", face_only=face_only)
        assert e.value.status_code == 503
        assert fragment in e.value.detail

    run(pg, body)
