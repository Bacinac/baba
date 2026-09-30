"""A throwaway Postgres for the tests that can only be told apart in real SQL.

tests/run.sh starts one — the image docker-compose.yml runs — and hands its
address in BABA_TEST_DSN. The migrations run once into a template; every test
gets a fresh copy of it, so no test sees another's rows. Asking for `pg`
without a database is a failure, never a skip: a database test that skips
when there is no database passes exactly when it proves nothing.
"""

import asyncio
import os
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import pytest

MIGRATIONS = Path(__file__).resolve().parent.parent / "db" / "migrations"
TEMPLATE = "baba_template"


def _with_database(dsn: str, name: str) -> str:
    return urlsplit(dsn)._replace(path=f"/{name}").geturl()


@pytest.fixture(scope="session")
def pg_server() -> str:
    dsn = os.environ.get("BABA_TEST_DSN")
    if not dsn:
        pytest.fail("BABA_TEST_DSN is not set — run the database tests through tests/run.sh")

    import asyncpg
    from baba_api.db import apply_migrations

    async def build() -> None:
        admin = await asyncpg.connect(dsn)
        try:
            await admin.execute(f"DROP DATABASE IF EXISTS {TEMPLATE}")
            await admin.execute(f"CREATE DATABASE {TEMPLATE}")
        finally:
            await admin.close()
        pool = await asyncpg.create_pool(_with_database(dsn, TEMPLATE), min_size=1, max_size=1)
        try:
            await apply_migrations(pool, MIGRATIONS)
        finally:
            await pool.close()

    asyncio.run(build())
    return dsn


@pytest.fixture
def pg(pg_server):
    """DSN of a freshly migrated database of this test's own."""
    import asyncpg

    name = f"t_{uuid.uuid4().hex}"

    async def admin(sql: str) -> None:
        conn = await asyncpg.connect(pg_server)
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    asyncio.run(admin(f"CREATE DATABASE {name} TEMPLATE {TEMPLATE}"))
    yield _with_database(pg_server, name)
    asyncio.run(admin(f"DROP DATABASE {name} WITH (FORCE)"))
