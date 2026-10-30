"""A redrawn scene region starts untaught.

Its references are embeddings of the old crop. The gate's polygon was moved
off the post onto the leaf on 30.10 with its 81 references still in place; set
against the new crop they would have voted for whatever the two crops share.
"""

import asyncio
import json
from types import SimpleNamespace

import asyncpg
from baba_api.models import SceneRegionPatch
from baba_api.routes_scene_states import patch_scene_region

POST = [[0.230, 0.483], [0.234, 0.645], [0.272, 0.641], [0.259, 0.484]]
LEAF = [[0.09, 0.51], [0.09, 0.645], [0.225, 0.622], [0.225, 0.51]]


def _run(pg: str, tmp_path, body) -> None:
    async def main():
        pool = await asyncpg.create_pool(pg, min_size=1, max_size=2)
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            pool=pool, config=SimpleNamespace(media_path=str(tmp_path)))))
        try:
            cam = await pool.fetchval(
                "INSERT INTO cameras (name, slug, stream_url) "
                "VALUES ('gate', 'gate', 'rtsp://cam/main') RETURNING id")
            rid = await pool.fetchval(
                "INSERT INTO scene_regions (camera_id, name, polygon, states) "
                "VALUES ($1, 'Gate', $2::jsonb, '{open,closed}') RETURNING id",
                cam, json.dumps(POST))
            crops = tmp_path / "scene_crops"
            crops.mkdir()
            for label in ("open", "closed"):
                name = await pool.fetchval(
                    "INSERT INTO scene_region_prototypes (region_id, state_label, crop_path) "
                    "VALUES ($1, $2, 'scene_crops/' || gen_random_uuid() || '.jpg') "
                    "RETURNING crop_path", rid, label)
                (tmp_path / name).write_bytes(b"jpeg")
            await body(pool, request, rid, crops)
        finally:
            await pool.close()

    asyncio.run(main())


def test_a_new_polygon_drops_the_references_of_the_old_one(pg, tmp_path):
    async def body(pool, request, rid, crops):
        await patch_scene_region(rid, SceneRegionPatch(polygon=LEAF), request, None)
        assert await pool.fetchval(
            "SELECT count(*) FROM scene_region_prototypes WHERE region_id = $1", rid) == 0
        assert list(crops.iterdir()) == []
        audit = json.loads(await pool.fetchval(
            "SELECT payload FROM admin_audit WHERE resource_id = $1", rid))
        assert audit["prototypes_dropped"] == 2

    _run(pg, tmp_path, body)


def test_the_same_polygon_or_another_field_keeps_them(pg, tmp_path):
    async def body(pool, request, rid, crops):
        await patch_scene_region(rid, SceneRegionPatch(polygon=POST), request, None)
        await patch_scene_region(rid, SceneRegionPatch(name="Kapija"), request, None)
        assert await pool.fetchval(
            "SELECT count(*) FROM scene_region_prototypes WHERE region_id = $1", rid) == 2
        assert len(list(crops.iterdir())) == 2

    _run(pg, tmp_path, body)
