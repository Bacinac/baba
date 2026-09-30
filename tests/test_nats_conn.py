"""A close we asked for is shutdown; a close the broker forced is a dead bus.

nats.py runs the same callbacks for both, and the dead-bus one sends the
process SIGTERM. From 23.09 every SSE stream a browser left drained its
connection straight and restarted the api with it — twice on production the
day after.
"""

import asyncio
import logging
import re
import signal
from pathlib import Path

from baba_core import nats_conn

ROOT = Path(__file__).resolve().parent.parent


class _Client:
    def __init__(self, callbacks: dict) -> None:
        self.callbacks = callbacks
        self.is_closed = False

    async def drain(self) -> None:
        self.is_closed = True
        await self.callbacks["disconnected_cb"]()
        await self.callbacks["closed_cb"]()


def _connected(monkeypatch):
    kills = []
    monkeypatch.setattr(nats_conn.os, "kill", lambda pid, sig: kills.append(sig))

    async def fake_connect(url, **kwargs):
        return _Client(kwargs)

    monkeypatch.setattr(nats_conn.nats, "connect", fake_connect)
    return kills


def test_our_own_drain_is_neither_a_warning_nor_a_death(monkeypatch, caplog):
    kills = _connected(monkeypatch)

    async def main():
        nc = await nats_conn.connect("nats://x", name="api-sse-det")
        await nats_conn.drain_quietly(nc)

    with caplog.at_level(logging.WARNING, logger=nats_conn.__name__):
        asyncio.run(main())
    assert kills == []
    assert caplog.records == []


def test_a_close_the_broker_forced_still_takes_the_process_down(monkeypatch, caplog):
    kills = _connected(monkeypatch)

    async def main():
        nc = await nats_conn.connect("nats://x", name="tracker")
        await nc.callbacks["closed_cb"]()

    with caplog.at_level(logging.ERROR, logger=nats_conn.__name__):
        asyncio.run(main())
    assert kills == [signal.SIGTERM]
    assert "closed for good (tracker)" in caplog.text


def test_no_service_closes_a_nats_connection_any_other_way():
    direct = re.compile(r"\b\w*nc\.(?:drain|close)\(\)")
    offenders = [
        f"{path.relative_to(ROOT)}:{n}"
        for tree in ("services", "core")
        for path in (ROOT / tree).rglob("*.py")
        if path.name != "nats_conn.py"
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if direct.search(line)
    ]
    assert offenders == []
