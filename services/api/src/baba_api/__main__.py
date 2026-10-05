from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import asyncpg
import uvicorn
from baba_core import (
    drain_quietly,
    make_embedding_backend,
    setup_logging,
)
from baba_core.nats_conn import connect as nats_connect
from baba_core.task_owner import TaskOwner
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from home_core.auth import bootstrap_admin_if_empty, load_or_create_secret
from home_core.tasks import spawn

from baba_api.auth import (
    peer_or_operator_writes,
    require_role,
    require_role_for_writes,
)
from baba_api.camera_ctl import serve_profile_requests, sweep_overrides
from baba_api.config import ApiConfig
from baba_api.db import apply_migrations
from baba_api.deliveries_sweeper import run_sweeper as run_deliveries_sweeper
from baba_api.events_bridge import EventsNatsBridge
from baba_api.events_retention_sweeper import run_sweeper as run_events_retention_sweeper
from baba_api.face_activation import ApiFaceActivation
from baba_api.face_recompute_queue import recover_recomputes, run_recompute_queue
from baba_api.go2rtc_sync import Go2RtcSync
from baba_api.live_view import TrackCache, live_router
from baba_api.revision import revision, version_string
from baba_api.roster_bridge import RosterNatsBridge
from baba_api.routes import router
from baba_api.routes_ai import ai_router
from baba_api.routes_analytics import analytics_router
from baba_api.routes_audit import audit_router
from baba_api.routes_auth import auth_router
from baba_api.routes_camera_light import camera_light_router
from baba_api.routes_detection_rules import detection_rules_router
from baba_api.routes_discovery import discovery_router
from baba_api.routes_events import events_router
from baba_api.routes_face_recognition import _run_recompute, face_recognition_router
from baba_api.routes_identities import auto_describe_loop, identities_router
from baba_api.routes_notifications import notifications_router
from baba_api.routes_probe import probe_router
from baba_api.routes_recordings import recordings_router
from baba_api.routes_rules import rules_router
from baba_api.routes_scene_states import scene_states_router
from baba_api.routes_search import search_router
from baba_api.routes_sightings import sightings_router
from baba_api.routes_stats import stats_router
from baba_api.routes_telemetry import telemetry_router
from baba_api.routes_thumbnails import thumbnails_router
from baba_api.routes_tunables import tunables_router
from baba_api.routes_users import users_router
from baba_api.routes_zones import zones_router
from baba_api.rules_dispatcher import RulesDispatcher
from baba_api.sse import sse_router
from baba_api.stats_aggregator import StatsAggregator
from baba_api.telemetry_analyzer import run_auto_analyzer
from baba_api.tracks_retention_sweeper import run_sweeper as run_tracks_retention_sweeper

log = logging.getLogger("baba.api")


# How often a camera held past its deadline is put back. Well under the
# shortest override anyone asks for (the headlight swap wants ~30 s), because
# the cost of a late restore is the operator's own night view.
_OVERRIDE_SWEEP_S = 5.0


async def _camera_override_sweeper(pool, stop: asyncio.Event) -> None:
    """Put back every camera whose temporary profile has run out."""
    while not stop.is_set():
        try:
            await sweep_overrides(pool)
        except Exception:
            log.exception("camera override sweep failed — continuing")
        with suppress(TimeoutError, asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=_OVERRIDE_SWEEP_S)


async def _started(start: Awaitable[object], failure: str) -> bool:
    """Start an optional part of the api; a failure is logged and boot goes on
    without it."""
    try:
        await start
    except Exception:
        log.exception(failure)
        return False
    return True


def _init_embedder_backend(app: FastAPI, config: ApiConfig) -> None:
    app.state.embedder_backend = None
    if not config.embedder_model_path:
        return
    try:
        app.state.embedder_backend = make_embedding_backend(Path(config.embedder_model_path))
    except Exception:
        log.exception("failed to initialise embedder backend; reference-photo upload disabled")


async def _start_bus_consumers(
    app: FastAPI, config: ApiConfig, pool
) -> tuple[EventsNatsBridge | None, RosterNatsBridge | None, TrackCache]:
    # Shared NATS connection for request/reply control calls. Today only the
    # scene-state router uses it (capture a reference / live-test a region,
    # both delegated to the state-evaluator which owns frame-ring + inference).
    # Failure just makes those two operations return 503 — never blocks boot.

    try:
        app.state.nats = await nats_connect(config.nats_url, name="api-control")
        log.info("api control NATS connected")
    except Exception:
        log.exception("api control NATS connect failed — scene-state capture/test disabled")
        app.state.nats = None

    # Live-grid track cache: one `baba.tracks.*` subscription feeding the
    # live-grid overlay (services/api/live_view.py). Boxes are painted onto the
    # detector's own SHM frames, so the grid never touches a video decoder.
    track_cache = TrackCache()
    if app.state.nats is None:
        return None, None, track_cache

    # Events → NATS bridge: mirror every `events` row onto baba.events.* so the
    # DIDA automation runtime (and any other off-box consumer) can react to
    # zone/track/scene-state events over the bus. Needs the shared NATS conn.
    events_bridge: EventsNatsBridge | None = EventsNatsBridge(config.dsn, pool, app.state.nats)
    if not await _started(
        events_bridge.start(), "events→NATS bridge failed to start — DIDA won't see events"
    ):
        events_bridge = None

    # Roster → NATS bridge: publish the full camera + zone inventory onto
    # baba.roster so DIDA can show every zone (not just ones that have fired an
    # event) and which classes each is tracking. Same shared NATS conn.
    roster_bridge: RosterNatsBridge | None = RosterNatsBridge(config.dsn, pool, app.state.nats)
    if not await _started(
        roster_bridge.start(),
        "roster→NATS bridge failed to start — DIDA won't see the full zone roster",
    ):
        roster_bridge = None

    await _started(
        track_cache.start(app.state.nats), "live track cache failed to start — grid boxes disabled"
    )
    return events_bridge, roster_bridge, track_cache


async def _recover_camera_overrides(pool) -> None:
    # A camera held in a temporary profile by a process that is no longer
    # running. The row survived the restart, so the deadline it carried is
    # meaningless and the promise to undo it was broken: put every one of them
    # back NOW, before anything else starts writing to cameras. Left alone, the
    # operator's night view stays swapped out until somebody notices.
    try:
        recovered = await sweep_overrides(pool, expired_only=False)
    except Exception:
        log.exception("camera override recovery failed — continuing")
        return
    if recovered:
        log.warning("restored %d camera(s) left in a temporary profile by a previous run", recovered)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config: ApiConfig = app.state.config
    pool = await asyncpg.create_pool(config.dsn, min_size=1, max_size=8)
    if config.migrations_dir.exists():
        await apply_migrations(pool, config.migrations_dir)
    else:
        log.warning("Migrations dir %s missing — skipping", config.migrations_dir)
    app.state.pool = pool
    app.state.secret_key = load_or_create_secret(config.state_dir, "BABA_SECRET_KEY")
    await bootstrap_admin_if_empty(pool, config.state_dir, env_prefix="BABA")
    await recover_recomputes(pool)

    _init_embedder_backend(app, config)
    face_activation = ApiFaceActivation(app, pool, config.dsn)
    await face_activation.start()
    app.state.face_activation = face_activation
    face_recompute_stop = asyncio.Event()
    face_recompute_task = spawn(
        run_recompute_queue(pool, face_recompute_stop, _run_recompute),
        name="face-recompute-queue", log=log,
    )

    # Live mirror of cameras → go2rtc. Failures are logged + retried; we never
    # block API startup on go2rtc being reachable.
    go2rtc = Go2RtcSync(config.dsn, config.go2rtc_url, auth=config.go2rtc_auth)
    await _started(
        go2rtc.start(), "go2rtc sync failed to start — frontend live view may lag for new cameras"
    )
    app.state.go2rtc = go2rtc

    # Auto-describe loop: opt-in via env (BABA_AI_AUTO_DESCRIBE=1). Seeds
    # unlabelled identities with an AI-generated draft so the gallery is
    # populated with names + plates instead of bare UUIDs.
    auto_task = spawn(auto_describe_loop(app), name="auto-describe-loop", log=log)

    # Rules dispatcher — LISTENs on events_new, matches active rules,
    # fans deliveries out through notification channels. Lives in api
    # so it has access to the same Fernet key that encrypts channel
    # configs (no separate decrypt path needed).
    dispatcher = RulesDispatcher(
        dsn=config.dsn,
        pool=pool,
        secret_key=app.state.secret_key,
    )
    await _started(
        dispatcher.start(), "rules dispatcher failed to start — notifications will not fire"
    )
    app.state.rules_dispatcher = dispatcher

    # Runtime stats aggregator — one NATS subscription to baba.stats.*,
    # keeps the latest snapshot + a short rolling window per pipeline
    # service in memory for Settings → System. Failure just leaves the
    # System metrics panel empty; never blocks API startup.
    stats_aggregator = StatsAggregator(config.nats_url)
    await _started(
        stats_aggregator.start(), "stats aggregator failed to start — System metrics will be empty"
    )
    app.state.stats_aggregator = stats_aggregator

    events_bridge, roster_bridge, track_cache = await _start_bus_consumers(app, config, pool)
    app.state.events_bridge = events_bridge
    app.state.roster_bridge = roster_bridge
    app.state.track_cache = track_cache

    await _recover_camera_overrides(pool)
    if app.state.nats is not None:
        await _started(
            serve_profile_requests(app.state.nats, pool),
            "camera profile service could not start — continuing",
        )

    camera_override_stop = asyncio.Event()
    camera_override_task = spawn(
        _camera_override_sweeper(pool, camera_override_stop),
        name="camera-override-sweeper",
        log=log,
    )

    # Deliveries retention sweeper. Keeps notification_deliveries from
    # growing unbounded over months. Disabled with
    # BABA_DELIVERIES_RETENTION_DAYS=0 if operator wants forever-history.
    deliveries_stop = asyncio.Event()
    deliveries_task = spawn(
        run_deliveries_sweeper(pool, deliveries_stop),
        name="deliveries-sweeper",
        log=log,
    )

    # Tracks retention sweeper. Walks expired `tracks.retain_until` rows
    # and unlinks the matching jpegs in lock-step with the DB delete —
    # this is the only thing standing between us and the previous "ghost
    # crop_path" bug where external media cleanup left dangling DB
    # references. Disabled with BABA_TRACKS_RETENTION_SWEEP_INTERVAL_S=0.
    tracks_retention_stop = asyncio.Event()
    tracks_retention_task = spawn(
        run_tracks_retention_sweeper(pool, config.media_path, tracks_retention_stop),
        name="tracks-retention-sweeper",
        log=log,
    )

    # Events retention sweeper. Bounds the append-only events table (age) and
    # clears tombstoned events whose track was already pruned. Disabled with
    # BABA_EVENTS_RETENTION_DAYS=0.
    events_retention_stop = asyncio.Event()
    events_retention_task = spawn(
        run_events_retention_sweeper(pool, events_retention_stop),
        name="events-retention-sweeper",
        log=log,
    )

    # Telemetry incident auto-analyzer. Idles until the operator enables
    # the app_settings switch (Settings → System → incidents panel), then
    # runs the AI verdict on newly opened incidents.
    auto_analyze_stop = asyncio.Event()
    auto_analyze_task = spawn(
        run_auto_analyzer(
            pool,
            secret_key=app.state.secret_key,
            go2rtc_url=config.go2rtc_url,
            go2rtc_auth=config.go2rtc_auth,
            stop=auto_analyze_stop,
        ),
        name="telemetry-auto-analyzer",
        log=log,
    )

    log.info("api ready: %s:%d", config.host, config.port)
    try:
        yield
    finally:
        face_recompute_stop.set()
        face_recompute_task.cancel()
        await face_activation.stop()
        with suppress(asyncio.CancelledError):
            await face_recompute_task
        camera_override_stop.set()
        with suppress(asyncio.CancelledError, Exception):
            await camera_override_task
        # Hand every held camera back on the way out. The next start would
        # recover them anyway, but a clean shutdown should not leave the
        # operator's night view swapped out for however long that takes.
        with suppress(Exception):
            await sweep_overrides(pool, expired_only=False)
        deliveries_stop.set()
        with suppress(asyncio.CancelledError, Exception):
            await deliveries_task
        tracks_retention_stop.set()
        with suppress(asyncio.CancelledError, Exception):
            await tracks_retention_task
        events_retention_stop.set()
        with suppress(asyncio.CancelledError, Exception):
            await events_retention_task
        auto_analyze_stop.set()
        with suppress(asyncio.CancelledError, Exception):
            await auto_analyze_task
        auto_task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await auto_task
        with suppress(Exception):
            await dispatcher.stop()
        with suppress(Exception):
            await stats_aggregator.stop()
        if events_bridge is not None:
            with suppress(Exception):
                await events_bridge.stop()
        if roster_bridge is not None:
            with suppress(Exception):
                await roster_bridge.stop()
        with suppress(Exception):
            await track_cache.stop()
        await app.state.background_tasks.stop(cancel=False)
        if app.state.nats is not None:
            with suppress(Exception):
                await drain_quietly(app.state.nats)
        with suppress(Exception):
            await go2rtc.stop()
        await pool.close()


def build_app(config: ApiConfig) -> FastAPI:
    app = FastAPI(title="BABA API", version=version_string(), lifespan=lifespan)
    app.state.config = config
    app.state.background_tasks = TaskOwner("api-background", log)

    # CORS only matters when the UI is on a different origin. In dev the
    # SvelteKit proxy is same-origin (everything goes through :5173), so the
    # dev defaults from config keep that working out of the box. Production
    # deployments set BABA_CORS_ORIGINS to their public origin(s) — every
    # other origin gets a CORS reject. "*" allowed for unauthenticated
    # probes but cookies won't travel (browser refuses credentials + *).
    cors_origins = list(config.cors_origins)
    cors_kwargs: dict = {
        "allow_credentials": True,
        "allow_methods": ["*"],
        "allow_headers": ["*"],
    }
    if cors_origins == ["*"]:
        # Mutually exclusive with allow_credentials per the CORS spec —
        # opt out of credentials so the wildcard actually takes effect.
        cors_kwargs["allow_credentials"] = False
        cors_kwargs["allow_origins"] = ["*"]
        log.warning(
            "CORS allow_origins=* with credentials disabled — cookie auth "
            "will NOT work cross-origin in this mode"
        )
    else:
        cors_kwargs["allow_origins"] = cors_origins
    app.add_middleware(CORSMiddleware, **cors_kwargs)
    log.info("CORS origins: %s", cors_origins)

    # Session cookies are HttpOnly + SameSite=Lax. The Secure flag is
    # controlled by BABA_COOKIE_SECURE — required behind any HTTPS
    # frontend, harmful behind plain HTTP (browser drops the cookie).
    # Warn loudly so an operator who forgot to flip this when going to
    # production sees it at boot rather than after a mystery logout bug.
    cookie_secure = os.environ.get("BABA_COOKIE_SECURE", "false").lower() in ("1", "true", "yes")
    if not cookie_secure:
        log.warning(
            "BABA_COOKIE_SECURE=false — session cookie will travel over plain HTTP. "
            "Safe for localhost dev; set BABA_COOKIE_SECURE=true behind a TLS reverse proxy."
        )

    # Auth routes are public (login/logout) or self-auth'd (/auth/me).
    app.include_router(auth_router)

    # Everything else requires a valid session cookie AND role-based access.
    # Dependencies are attached at router-include time (method-aware), so a
    # `viewer` can browse read endpoints but never mutate, while config /
    # secret-bearing routers additionally require `admin` to write.
    #
    #   op_writes    : any authed user reads; operator+ mutates
    #   admin_writes : any authed user reads; admin only mutates (config/secrets)
    #   admin_only   : admin for every method (reads are privileged too)
    op_writes = [Depends(require_role_for_writes("operator"))]
    admin_writes = [Depends(require_role_for_writes("admin"))]
    admin_only = [Depends(require_role("admin"))]

    # Operational data — operator may mutate, viewer read-only.
    # The one surface a peer key may write to. DIDA measures the light outside
    # and this turns the camera's floodlight on for it; every other peer request
    # stays read-only. See `peer_or_operator_writes` for why that is its own
    # dependency and not a flag.
    app.include_router(
        camera_light_router, dependencies=[Depends(peer_or_operator_writes())]
    )
    app.include_router(events_router, dependencies=op_writes)
    app.include_router(sightings_router, dependencies=op_writes)
    app.include_router(face_recognition_router, dependencies=op_writes)
    app.include_router(identities_router, dependencies=op_writes)
    app.include_router(recordings_router, dependencies=op_writes)
    app.include_router(thumbnails_router, dependencies=op_writes)
    app.include_router(zones_router, dependencies=op_writes)
    app.include_router(scene_states_router, dependencies=op_writes)
    app.include_router(stats_router, dependencies=op_writes)
    app.include_router(search_router, dependencies=op_writes)
    app.include_router(analytics_router, dependencies=op_writes)

    # Config / infrastructure / secrets — admin to mutate, viewer read-only.
    # `router` (routes.py) carries camera CRUD; discovery/probe are internal
    # network scanners (SSRF surface); ai/notifications hold provider keys and
    # channel secrets; detection_rules/rules are global pipeline config.
    app.include_router(router, dependencies=admin_writes)
    app.include_router(discovery_router, dependencies=admin_writes)
    app.include_router(probe_router, dependencies=admin_writes)
    app.include_router(detection_rules_router, dependencies=admin_writes)
    app.include_router(tunables_router, dependencies=admin_writes)
    app.include_router(ai_router, dependencies=admin_writes)
    # analyze spends AI credits and apply mutates camera settings/rules —
    # admin gate for writes, same policy as the rest of the AI surface.
    app.include_router(telemetry_router, dependencies=admin_writes)
    app.include_router(notifications_router, dependencies=admin_writes)
    app.include_router(rules_router, dependencies=admin_writes)

    # Admin-only end to end — user records and the audit trail are privileged
    # to read as well as write.
    app.include_router(users_router, dependencies=admin_only)
    app.include_router(audit_router, dependencies=admin_only)

    app.include_router(sse_router)  # SSE feeds; each route auths via current_user
    app.include_router(live_router)  # live grid stills; auths via current_user (cookie)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    @app.get("/version")
    async def version() -> dict:
        # Public (like /healthz). Single source: revision.json, git-stamped by the
        # push hook (scripts/gen-revision.sh) and bind-mounted read-only. Read per
        # request so a push refreshes it without restarting the api.
        return revision()

    return app


def main() -> None:
    setup_logging("api")
    config = ApiConfig.from_env()
    app = build_app(config)
    uvicorn.run(
        app,
        host=config.host,
        port=config.port,
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
