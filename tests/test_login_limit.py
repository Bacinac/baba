"""Five password guesses a minute per client, ten per account from anywhere:
the login route is wired to both limits. The buckets and the client address
are home_core's and tested there; a limiter that exists but is not in the
request path limits nothing.
"""

import pytest
from baba_api import rate_limit
from baba_api.__main__ import build_app
from baba_api.config import ApiConfig
from fastapi.testclient import TestClient


class _NoUsers:
    async def fetchrow(self, *_a, **_kw):
        return None


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("POSTGRES_PASSWORD", "unused")
    rate_limit.LOGIN.clear()
    built = build_app(ApiConfig.from_env())
    built.state.pool = _NoUsers()
    return built


def _login(client, username="marko", **kw):
    return client.post("/auth/login", json={"username": username, "password": "guess"}, **kw)


def test_the_sixth_guess_from_one_client_is_refused(app):
    client = TestClient(app, client=("192.168.10.50", 50000))
    assert [_login(client).status_code for _ in range(5)] == [401] * 5
    r = _login(client)
    assert r.status_code == 429
    assert r.headers["Retry-After"] == "15"


def test_a_direct_caller_cannot_forge_fresh_addresses(app):
    client = TestClient(app, client=("192.168.10.50", 50000))
    codes = [
        _login(client, headers={"X-Forwarded-For": f"203.0.113.{i}"}).status_code
        for i in range(6)
    ]
    assert codes == [401] * 5 + [429]


def test_one_account_is_guarded_across_rotating_clients(app):
    codes = [
        _login(TestClient(app, client=(f"198.51.100.{i}", 50000)), username=("marko", "Marko", " MARKO ")[i % 3]).status_code
        for i in range(11)
    ]
    assert codes == [401] * 10 + [429]
    other = TestClient(app, client=("198.51.100.200", 50000))
    assert _login(other, username="ana").status_code == 401
