import asyncio
import logging
from contextlib import suppress

log = logging.getLogger(__name__)


async def queue_selection_recompute(conn, previous, selection, user_id):
    pair = (selection["model_key"], selection["detector_key"])
    if pair == (previous["model_key"], previous["detector_key"]) and pair == (
        previous["active_model_key"], previous["active_detector_key"]
    ):
        await conn.execute(
            "UPDATE face_recompute_jobs SET settings_revision = $1 WHERE status = 'pending'",
            selection["revision"],
        )
        return
    await conn.execute(
        "UPDATE face_recompute_jobs SET status = 'cancelled', finished_at = now(), "
        "error_message = 'superseded by a newer face selection' WHERE status = 'pending'"
    )
    await conn.execute(
        "INSERT INTO face_recompute_jobs(model_key, detector_key, settings_revision, started_by, status) "
        "VALUES($1, $2, $3, $4, 'pending')",
        *pair, selection["revision"], user_id,
    )


async def claim_recompute(pool):
    async with pool.acquire() as conn, conn.transaction():
        selection = await conn.fetchrow("SELECT * FROM face_recognition_settings WHERE id = 1 FOR UPDATE")
        job = await conn.fetchrow("SELECT * FROM face_recompute_jobs WHERE status = 'pending' FOR UPDATE")
        if job is None:
            return None
        if (job["settings_revision"] != selection["revision"]
                or job["model_key"] != selection["model_key"]
                or job["detector_key"] != selection["detector_key"]):
            await conn.execute(
                "UPDATE face_recompute_jobs SET status = 'cancelled', finished_at = now(), "
                "error_message = 'superseded by a newer face selection' WHERE id = $1", job["id"],
            )
            return None
        if (selection["revision"] != selection["active_revision"]
                or selection["api_error"] or selection["embedder_error"]
                or await conn.fetchval("SELECT EXISTS(SELECT 1 FROM face_recompute_jobs WHERE status = 'running')")):
            return None
        await conn.execute("UPDATE face_recompute_jobs SET status = 'running', started_at = now() WHERE id = $1", job["id"])
        return job


async def recover_recomputes(pool):
    async with pool.acquire() as conn, conn.transaction():
        selection = await conn.fetchrow("SELECT * FROM face_recognition_settings WHERE id = 1 FOR UPDATE")
        job = await conn.fetchrow("SELECT * FROM face_recompute_jobs WHERE status = 'running' FOR UPDATE")
        if job is None:
            return
        pending = await conn.fetchval("SELECT EXISTS(SELECT 1 FROM face_recompute_jobs WHERE status = 'pending')")
        if not pending and (job["model_key"], job["detector_key"]) == (selection["model_key"], selection["detector_key"]):
            await conn.execute(
                "UPDATE face_recompute_jobs SET status = 'pending', settings_revision = $2, "
                "total = 0, processed = 0, succeeded = 0, no_face = 0, missing_file = 0, "
                "error_message = NULL, finished_at = NULL WHERE id = $1", job["id"], selection["revision"],
            )
        else:
            await conn.execute(
                "UPDATE face_recompute_jobs SET status = 'failed', "
                "error_message = 'interrupted by an api restart', finished_at = now() WHERE id = $1", job["id"],
            )
        log.warning("interrupted face recompute reconciled job=%s", job["id"])


async def run_recompute_queue(pool, stop: asyncio.Event, execute):
    while not stop.is_set():
        try:
            await recover_recomputes(pool)
            job = await claim_recompute(pool)
            if job is not None:
                await execute(pool, job["id"], job["model_key"], job["detector_key"])
        except Exception:
            log.exception("face recompute queue failed")
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=1.0)
