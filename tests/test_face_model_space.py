"""An identity's face centroid stays inside the active embedder's space.

Enrolment and the recompute job both call `_recompute_label_aggregates`; an
average across two models' vectors is a point in neither, and the matchers
only trust a vector whose `face_embedding_model` names the active model.
"""

import asyncio
from uuid import uuid4

import asyncpg
from baba_api.routes_identities._base import _recompute_label_aggregates


def vec(axis: int) -> str:
    v = [0.0] * 512
    v[axis] = 1.0
    return "[" + ",".join(map(repr, v)) + "]"


def floats(v: str) -> list[float]:
    return [float(x) for x in v.strip("[]").split(",")]


def test_the_centroid_averages_only_the_active_models_faces(pg):
    async def main():
        conn = await asyncpg.connect(pg)
        try:
            await conn.execute(
                "UPDATE face_recognition_settings SET active_model_key = 'active', model_key = 'pending' WHERE id = 1"
            )

            async def identity(*photos) -> object:
                gid = uuid4()
                await conn.execute(
                    "INSERT INTO identity_labels (global_id, name, kind) VALUES ($1, $2, 'person')",
                    gid, str(gid),
                )
                for face, model in photos:
                    await conn.execute(
                        """
                        INSERT INTO identity_reference_photos
                            (global_id, photo_path, face_embedding, face_embedding_model, body_embedding)
                        VALUES ($1, 'reference_photos/x.jpg', $2::text::vector, $3, $4::text::vector)
                        """,
                        gid, face, model, vec(0),
                    )
                await _recompute_label_aggregates(conn, gid)
                return await conn.fetchrow(
                    "SELECT face_embedding::text AS face, face_embedding_model, reference_count "
                    "FROM identity_labels WHERE global_id = $1",
                    gid,
                )

            mixed = await identity((vec(10), "active"), (vec(20), "old"), (vec(30), None))
            assert floats(mixed["face"]) == floats(vec(10))
            assert (mixed["face_embedding_model"], mixed["reference_count"]) == ("active", 3)

            stale = await identity((vec(20), "old"), (vec(30), None))
            assert (stale["face"], stale["face_embedding_model"], stale["reference_count"]) == (None, None, 2)
        finally:
            await conn.close()

    asyncio.run(main())
