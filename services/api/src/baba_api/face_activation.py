from __future__ import annotations

import asyncio
import logging
import os
from contextlib import suppress
from pathlib import Path

import asyncpg
from baba_core import make_face_stack_for_model
from baba_core.face_settings import acknowledge_face_selection, read_face_selection
from baba_core.native import run_native
from baba_core.pg_listen import ResilientListener
from fastapi import FastAPI
from home_core.tasks import spawn

log = logging.getLogger("baba.api")


class ApiFaceActivation:
    _RETRY_INTERVAL_S = 5.0

    def __init__(self, app: FastAPI, pool: asyncpg.Pool, dsn: str) -> None:
        self._app = app
        self._pool = pool
        self._lock = asyncio.Lock()
        self._requested = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._retry_needed = False
        self._listener = ResilientListener(
            dsn,
            ["face_recognition_changed"],
            on_notify=lambda _channel, _payload: self.request(),
            on_connect=self.refresh,
            name="api-face-settings",
        )

    async def start(self) -> None:
        await self._listener.start()
        self._task = spawn(self._run(), name="api-face-stack-reload", log=log)

    def request(self) -> None:
        if not self._stopping:
            self._requested.set()

    async def stop(self) -> None:
        self._stopping = True
        await self._listener.stop()
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while True:
            if self._retry_needed:
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._requested.wait(), self._RETRY_INTERVAL_S)
            else:
                await self._requested.wait()
            self._requested.clear()
            try:
                await self.refresh()
            except Exception:
                log.exception("api face settings refresh failed")
                await asyncio.sleep(1.0)
                self.request()

    async def refresh(self) -> None:
        async with self._lock:
            if self._stopping:
                return
            selection = await read_face_selection(self._pool)
            pair = (selection.detector_key, selection.model_key)
            state = self._app.state
            path = os.environ.get("BABA_FACE_DETECTOR_MODEL", "").strip()
            try:
                if not path:
                    raise RuntimeError("Face detector is not configured")
                if getattr(state, "face_loaded_pair", None) != pair:
                    stack, detector, model = await run_native(
                        make_face_stack_for_model,
                        yunet_path=Path(path), models_dir=Path("/models"),
                        model_key=selection.model_key, detector_key=selection.detector_key,
                    )
                    if stack is None or (detector, model) != pair:
                        raise RuntimeError("selected face models could not be loaded")
                    state.face_stack = stack
                    state.face_model_key = model
                    state.face_loaded_pair = pair
                state.face_stack_error = None
            except Exception as exc:
                self._retry_needed = bool(path)
                self._disable(str(exc))
                if path:
                    log.exception("api face activation failed")
                accepted = await acknowledge_face_selection(self._pool, "api", selection, str(exc))
            else:
                self._retry_needed = False
                accepted = await acknowledge_face_selection(self._pool, "api", selection)
            if not accepted:
                self.request()

    def _disable(self, error: str) -> None:
        state = self._app.state
        state.face_stack = None
        state.face_model_key = None
        state.face_loaded_pair = None
        state.face_stack_error = error
