# syntax=docker/dockerfile:1
# One-shot that carries a database across a Postgres major upgrade before the
# server starts (docker-compose `postgres-upgrade`). pg_upgrade needs the old
# major's binaries next to the new one's, and both with pgvector, because it
# starts the old cluster to read its schema — hence the old major installed
# from the same PGDG repo the image already uses.
FROM pgvector/pgvector:pg18-trixie

ARG OLD_MAJOR=17
RUN sed -i "s/\$/ ${OLD_MAJOR}/" /etc/apt/sources.list.d/pgdg.list \
 && apt-get update && apt-get install -y --no-install-recommends \
    "postgresql-${OLD_MAJOR}" "postgresql-${OLD_MAJOR}-pgvector" \
 && rm -rf /var/lib/apt/lists/*

COPY docker/postgres-upgrade.sh /usr/local/bin/postgres-upgrade
ENTRYPOINT ["postgres-upgrade"]
