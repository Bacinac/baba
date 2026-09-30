"""Postgres DSN from the standard BABA environment.

Every service composes the same DSN from the same POSTGRES_* variables;
this helper replaces the per-service copies of that block so the
convention (including POSTGRES_SSLMODE handling) lives in one place.

POSTGRES_SSLMODE only matters when pointing at an external TLS-enforcing
Postgres (RDS, managed cluster). Empty = asyncpg default behaviour, which
is right for the bundled non-TLS pg.
"""

from __future__ import annotations

import os


def dsn_from_env() -> str:
    pg_user = os.environ.get("POSTGRES_USER", "baba")
    pg_pass = os.environ.get("POSTGRES_PASSWORD", "changeme")
    pg_db = os.environ.get("POSTGRES_DB", "baba")
    pg_host = os.environ.get("POSTGRES_HOST", "postgres")
    pg_port = os.environ.get("POSTGRES_PORT", "5432")
    pg_ssl = os.environ.get("POSTGRES_SSLMODE", "").strip()
    suffix = f"?sslmode={pg_ssl}" if pg_ssl else ""
    return f"postgresql://{pg_user}:{pg_pass}@{pg_host}:{pg_port}/{pg_db}{suffix}"
