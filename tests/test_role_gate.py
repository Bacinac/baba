"""Who may do what is decided once per router, where build_app includes it.

A router included without its gate, or with the wrong one, is a hole no
handler test would ever see, so these walk every route the app actually
serves — a route added tomorrow is covered by the same rules the day it lands.
The gate runs before the handler, so a request the gate lets through fails
later on the missing database; only 401/403 are the gate's answer. A live
stream the gate lets through never ends, so the streams are held to the gate
only where it refuses.
"""

import re
from uuid import UUID

import pytest
from baba_api import auth
from baba_api.__main__ import build_app
from baba_api.auth import AuthUser, current_user
from baba_api.config import ApiConfig
from baba_api.routes import router as cameras_router
from baba_api.routes_ai import ai_router
from baba_api.routes_audit import audit_router
from baba_api.routes_camera_light import camera_light_router
from baba_api.routes_detection_rules import detection_rules_router
from baba_api.routes_discovery import discovery_router
from baba_api.routes_notifications import notifications_router
from baba_api.routes_probe import probe_router
from baba_api.routes_rules import rules_router
from baba_api.routes_telemetry import telemetry_router
from baba_api.routes_tunables import tunables_router
from baba_api.routes_users import users_router
from baba_api.sse import sse_router
from fastapi import FastAPI
from fastapi.testclient import TestClient
from home_core.auth import encode_session_token

PUBLIC = {
    ("POST", "/auth/login"),
    ("POST", "/auth/logout"),
    ("GET", "/healthz"),
    ("GET", "/version"),
}
SELF_SERVICE = {
    ("POST", "/auth/change-password"),
    ("PATCH", "/auth/preferences"),
    ("POST", "/auth/2fa/setup"),
    ("POST", "/auth/2fa/enable"),
    ("POST", "/auth/2fa/recovery-codes/regenerate"),
    ("POST", "/auth/2fa/disable"),
}
ADMIN_WRITES = (
    cameras_router, discovery_router, probe_router, detection_rules_router,
    tunables_router, ai_router, telemetry_router, notifications_router, rules_router,
)
ADMIN_ONLY = (users_router, audit_router)
# Admin-only one route at a time, inside routers an operator otherwise writes
# to: the stored API keys of the photo sources, and wiping the whole archive.
ADMIN_ROUTES = {
    ("PUT", "/immich/settings"),
    ("PUT", "/opus/settings"),
    ("DELETE", "/recordings"),
}
READS = {"GET"}


def _routes(spec: dict) -> set[tuple[str, str]]:
    return {(m.upper(), p) for p, ops in spec.items() for m in ops
            if m in ("get", "post", "put", "patch", "delete")}


def _router_routes(router) -> set[tuple[str, str]]:
    probe = FastAPI()
    probe.include_router(router)
    found = _routes(probe.openapi()["paths"])
    assert found, f"{router} serves no routes"
    return found


def _url(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "00000000-0000-0000-0000-000000000001", path)


@pytest.fixture(scope="module")
def app():
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("POSTGRES_PASSWORD", "unused")
        built = build_app(ApiConfig.from_env())
    yield built


@pytest.fixture(scope="module")
def routes(app):
    found = _routes(app.openapi()["paths"])
    assert len(found) > 100
    return found


@pytest.fixture
def client(app):
    app.dependency_overrides.clear()
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def _as(app, role: str, username: str = "someone") -> None:
    app.dependency_overrides[current_user] = lambda: AuthUser(
        id=UUID(int=7), username=username, role=role
    )


def _status(client, method: str, path: str, **kw) -> int:
    return client.request(method, _url(path), **kw).status_code


def test_without_a_session_only_the_front_door_answers(client, routes):
    leaked = [(m, p) for m, p in sorted(routes - PUBLIC) if _status(client, m, p) != 401]
    assert leaked == []


def test_a_network_scan_recognises_a_baba_by_its_version(client):
    """DIDA's discovery takes an address for a BABA on this field alone."""
    assert client.get("/version").json()["product"] == "baba"


def test_a_viewer_changes_nothing_but_its_own_account(app, client, routes):
    _as(app, "viewer")
    writes = {(m, p) for m, p in routes if m not in READS} - PUBLIC - SELF_SERVICE
    leaked = [(m, p) for m, p in sorted(writes) if _status(client, m, p) != 403]
    assert leaked == []


def test_a_viewer_reads_everything_but_users_and_audit(app, client, routes):
    _as(app, "viewer")
    admin_only = set().union(*(_router_routes(r) for r in ADMIN_ONLY))
    reads = {(m, p) for m, p in routes if m in READS} - PUBLIC - admin_only - _router_routes(sse_router)
    refused = [(m, p) for m, p in sorted(reads) if _status(client, m, p) in (401, 403)]
    assert refused == []


def test_config_and_secrets_are_written_only_by_an_admin(app, client, routes):
    config_writes = {
        (m, p) for r in ADMIN_WRITES for m, p in _router_routes(r) if m not in READS
    } | ADMIN_ROUTES
    assert config_writes <= routes
    _as(app, "operator")
    assert [x for x in sorted(config_writes) if _status(client, *x) != 403] == []
    _as(app, "admin")
    assert [x for x in sorted(config_writes) if _status(client, *x) in (401, 403)] == []


def test_users_and_audit_are_admin_even_to_read(app, client, routes):
    admin_only = set().union(*(_router_routes(r) for r in ADMIN_ONLY))
    assert admin_only <= routes
    _as(app, "operator")
    assert [x for x in sorted(admin_only) if _status(client, *x) != 403] == []
    _as(app, "admin")
    assert [x for x in sorted(admin_only) if _status(client, *x) in (401, 403)] == []


def test_an_operator_runs_the_house_but_not_its_configuration(app, client, routes):
    _as(app, "operator")
    admin = set().union(*(_router_routes(r) for r in ADMIN_WRITES + ADMIN_ONLY)) | ADMIN_ROUTES
    operational = {(m, p) for m, p in routes if m not in READS} - PUBLIC - admin
    refused = [x for x in sorted(operational) if _status(client, *x) in (401, 403)]
    assert refused == []


def test_a_peer_key_reads_and_writes_only_the_floodlight(client, routes, monkeypatch):
    monkeypatch.setenv("BABA_PEER_KEYS", "dida:s3cret")
    key = {"headers": {auth.PEER_HEADER: "s3cret"}}
    light = {x for x in _router_routes(camera_light_router) if x[0] not in READS}
    writes = {(m, p) for m, p in routes if m not in READS} - PUBLIC - light
    assert light
    assert [x for x in sorted(light) if _status(client, *x, **key) in (401, 403)] == []
    assert [x for x in sorted(writes) if _status(client, *x, **key) not in (401, 403)] == []


CAMERA = {
    "id": UUID(int=1), "slug": "gate", "name": "Kapija",
    "stream_url": "rtsp://admin:s3cret@192.0.2.12:554/h264Preview_01_main",
    "substream_url": "http://192.0.2.12/flv?port=1935&app=bcs&user=admin&password=s3cret",
    "analysis_stream": "main", "enabled": True, "target_fps": 4, "idle_fps": None,
    "stillness_ratio": None, "park_seconds": None, "lost_seconds": None,
    "reid_lost_seconds": None, "maintain_conf": 0.3, "light_condition": "normal",
    "downscale_max_edge": 0, "recording_enabled": True, "color": "#f59e0b",
    "created_at": "2026-09-24T12:00:00Z", "updated_at": "2026-09-24T12:00:00Z",
}


class _OneCamera:
    async def fetch(self, *_):
        return [CAMERA]

    async def fetchrow(self, *_):
        return CAMERA


def _camera_urls(client, **kw) -> list[str]:
    listed = client.get("/cameras", **kw).json()[0]
    one = client.get(f"/cameras/{CAMERA['id']}", **kw).json()
    return [c[k] for c in (listed, one) for k in ("stream_url", "substream_url")]


def test_only_an_admin_reads_a_cameras_credentials(app, client, monkeypatch):
    """The admin edits the stream URL, so the admin reads it whole; to everyone
    else, a peer included, it names the host and path but not the login."""
    monkeypatch.setattr(app.state, "pool", _OneCamera(), raising=False)
    _as(app, "admin")
    assert all("s3cret" in u for u in _camera_urls(client))
    for role in ("operator", "viewer"):
        _as(app, role)
        urls = _camera_urls(client)
        assert not any("s3cret" in u or "admin" in u for u in urls), role
        assert all("192.0.2.12" in u for u in urls)
    app.dependency_overrides.clear()
    monkeypatch.setenv("BABA_PEER_KEYS", "dida:k3y")
    urls = _camera_urls(client, headers={auth.PEER_HEADER: "k3y"})
    assert not any("s3cret" in u for u in urls)


def test_a_wrong_peer_key_is_nobody(client, monkeypatch):
    monkeypatch.setenv("BABA_PEER_KEYS", "dida:s3cret")
    r = client.get("/events", headers={auth.PEER_HEADER: "s3cre"})
    assert r.status_code == 401


def test_an_unknown_role_ranks_below_viewer():
    assert not auth.role_at_least("superuser", "viewer")
    assert not auth.role_at_least("", "viewer")
    assert auth.role_at_least("viewer", "viewer")
    assert not auth.role_at_least("viewer", "operator")
    assert auth.role_at_least("admin", "operator")


def test_a_session_minted_before_a_password_change_is_refused(client, monkeypatch):
    user = AuthUser(id=UUID(int=9), username="marko", role="admin", token_version=4)

    async def fetch(_pool, user_id):
        return user if user_id == user.id else None

    monkeypatch.setattr(auth, "fetch_user", fetch)
    secret = client.app.state.secret_key = "k" * 32
    stale = encode_session_token(user.id, secret, token_version=3, ttl=auth.TOKEN_TTL)
    fresh = encode_session_token(user.id, secret, token_version=4, ttl=auth.TOKEN_TTL)
    client.app.state.pool = None
    client.cookies.set(auth.SESSION_COOKIE.name, stale)
    assert client.get("/events").status_code == 401
    client.cookies.set(auth.SESSION_COOKIE.name, fresh)
    assert client.get("/events").status_code not in (401, 403)
