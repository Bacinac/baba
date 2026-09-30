from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import asyncpg

log = logging.getLogger(__name__)


def _read_migrations(migrations_dir: Path) -> list[tuple[str, str]]:
    return [(p.stem, p.read_text()) for p in sorted(migrations_dir.glob("*.sql"))]


async def apply_migrations(pool: asyncpg.Pool, migrations_dir: Path) -> None:
    """Apply any unapplied migration files in lexicographic order.

    Migration files are plain .sql files named `NNN_name.sql`. We track which
    versions have run in `schema_versions`. The very first migration must
    create that table; we treat it as a bootstrap special case.
    """
    migrations = await asyncio.to_thread(_read_migrations, migrations_dir)
    if not migrations:
        log.warning("No migration files found in %s", migrations_dir)
        return

    async with pool.acquire() as conn:
        # Bootstrap: schema_versions table may not exist yet.
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_versions (
                version    text        PRIMARY KEY,
                applied_at timestamptz NOT NULL DEFAULT now()
            );
            """
        )
        applied = {r["version"] for r in await conn.fetch("SELECT version FROM schema_versions")}

        for version, sql in migrations:
            if version in applied:
                continue
            log.info("Applying migration %s", version)
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO schema_versions (version) VALUES ($1) ON CONFLICT DO NOTHING",
                    version,
                )
