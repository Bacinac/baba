"""A region that cannot answer for minutes stops vouching for the state it holds.

Holding the last committed state through a withheld verdict is what keeps a
passing car from filing a transition. Held for minutes, it is a guess shown as
fact: on 05.10 the gate read `unknown` from 09:17 and DIDA showed it open over a
closed gate. What goes off-box is `published_state`, and only the evaluator's
own reads move it.
"""

import asyncio
import json
from types import SimpleNamespace

import asyncpg
from baba_state_evaluator.__main__ import RegionConfig, StateEvaluator

_POLYGON = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0]]


def _run(pg: str, body) -> None:
    async def main():
        pool = await asyncpg.create_pool(pg, min_size=1, max_size=3)
        try:
            await body(pool)
        finally:
            await pool.close()

    asyncio.run(main())


async def _gate_held_open(pool):
    cam = await pool.fetchval(
        "INSERT INTO cameras (name, slug, stream_url) VALUES ('gate', 'gate', 'rtsp://go2rtc/x') RETURNING id"
    )
    rid = await pool.fetchval(
        "INSERT INTO scene_regions (camera_id, name, polygon, states)"
        " VALUES ($1, 'Gate', $2::jsonb, '{open,closed}') RETURNING id",
        cam, json.dumps(_POLYGON),
    )
    region = RegionConfig(
        id=rid, camera_id=cam, camera_slug="gate", name="Gate", polygon=_POLYGON,
        states=["open", "closed"], sample_interval_s=2, hysteresis_n=2,
        unknown_margin=0.4, enabled=True,
    )
    ev = StateEvaluator.__new__(StateEvaluator)
    ev._pool = pool
    ev._stats = SimpleNamespace(incr=lambda *_: None)
    await ev._commit_transition(region, None, "open", 0.1)
    return ev, region


async def _status(pool, rid):
    return await pool.fetchrow(
        "SELECT current_state, published_state, unsure, unsure_since"
        " FROM scene_region_status WHERE region_id = $1", rid)


async def _minutes_pass(pool, rid):
    await pool.execute(
        "UPDATE scene_region_status SET unsure_since = now() - interval '4 minutes'"
        " WHERE region_id = $1", rid)


class _Dida:
    """What a consumer of `scene_status_changed` is told, in order."""

    def __init__(self, pool):
        self._pool = pool
        self.seen: list[str] = []

    def _on(self, *args):
        self.seen.append(json.loads(args[3])["state"])

    async def __aenter__(self):
        self._conn = await self._pool.acquire()
        await self._conn.add_listener("scene_status_changed", self._on)
        return self

    async def __aexit__(self, *exc):
        await asyncio.sleep(0.2)
        await self._conn.remove_listener("scene_status_changed", self._on)
        await self._pool.release(self._conn)


def test_a_short_withheld_verdict_still_publishes_the_held_state(pg):
    async def body(pool):
        ev, region = await _gate_held_open(pool)
        for _ in range(3):
            await ev._status_eval_tick(region.id, "unknown", 0.2)
        s = await _status(pool, region.id)
        assert s["published_state"] == "open"
        assert s["unsure_since"] is not None and not s["unsure"]

    _run(pg, body)


def test_minutes_without_an_answer_publish_unknown_and_keep_the_held_state(pg):
    async def body(pool):
        ev, region = await _gate_held_open(pool)
        async with _Dida(pool) as dida:
            await ev._status_eval_tick(region.id, "unknown", 0.2)
            await _minutes_pass(pool, region.id)
            await ev._status_eval_tick(region.id, "unknown", 0.2)
        s = await _status(pool, region.id)
        assert s["published_state"] == "unknown"
        assert s["current_state"] == "open"
        assert dida.seen == ["unknown"]

    _run(pg, body)


def test_a_new_label_pending_hysteresis_does_not_flash_the_held_state(pg):
    async def body(pool):
        ev, region = await _gate_held_open(pool)
        await ev._status_eval_tick(region.id, "unknown", 0.2)
        await _minutes_pass(pool, region.id)
        await ev._status_eval_tick(region.id, "unknown", 0.2)
        async with _Dida(pool) as dida:
            await ev._status_eval_tick(region.id, "closed", 0.03)
            assert (await _status(pool, region.id))["published_state"] == "unknown"
            await ev._commit_transition(region, "open", "closed", 0.03)
        s = await _status(pool, region.id)
        assert s["published_state"] == "closed"
        assert s["unsure_since"] is None and not s["unsure"]
        assert dida.seen == ["closed"]

    _run(pg, body)


def test_reading_the_held_state_again_vouches_for_it(pg):
    async def body(pool):
        ev, region = await _gate_held_open(pool)
        await ev._status_eval_tick(region.id, "unknown", 0.2)
        await _minutes_pass(pool, region.id)
        await ev._status_eval_tick(region.id, "unknown", 0.2)
        await ev._status_eval_tick(region.id, "open", 0.03)
        s = await _status(pool, region.id)
        assert s["published_state"] == "open"
        assert s["unsure_since"] is None and not s["unsure"]

    _run(pg, body)


def test_a_frozen_camera_is_no_answer_either(pg):
    async def body(pool):
        ev, region = await _gate_held_open(pool)
        await ev._status_mark_stale(region.id)
        await _minutes_pass(pool, region.id)
        await ev._status_mark_stale(region.id)
        assert (await _status(pool, region.id))["published_state"] == "unknown"

    _run(pg, body)
