#!/usr/bin/env bash
# Carries the database to this image's Postgres major, once, before the server
# starts. A no-op when the current cluster already exists or there is nothing
# to carry (a fresh install).
#
# The old cluster is copied, never linked or moved: it stays a working cluster
# of its own major until someone removes it, which is the way back if the new
# one turns out wrong.
set -euo pipefail

# The storage tier's state dir, mounted whole: the cluster before Postgres 18
# is state/postgres (the image mounted it straight at /var/lib/postgresql/data);
# from 18 on the server mounts state/postgresql at /var/lib/postgresql and keeps
# each major in <major>/docker there, side by side.
state=/state
legacy="$state/postgres"
root="$state/postgresql"
new="$root/$PG_MAJOR/docker"
stage="$new.upgrading"
user="${POSTGRES_USER:-postgres}"

log() { echo "postgres-upgrade: $*"; }
# Until the last step refreshes it, every connection to a database whose
# recorded collation version differs from the C library's warns about it —
# including the old server pg_upgrade starts here, which runs on this image's
# newer library to read the schema and never serves data again.
quiet_collation() {
    sed -E -e 's/WARNING:  database "[^"]*" has a collation version mismatch//' \
           -e '/^DETAIL:  The database was created using collation version/d' \
           -e '/^HINT:  Rebuild all objects in this database/d'
}
die() { echo "postgres-upgrade: $*" >&2; exit 1; }

if [ -s "$new/PG_VERSION" ]; then
    log "postgres $PG_MAJOR cluster in place — nothing to upgrade"
    exit 0
fi

old=()
for d in "$legacy" "$root"/*/docker; do
    if [ "$d" != "$new" ] && [ -s "$d/PG_VERSION" ]; then old+=("$d"); fi
done
case ${#old[@]} in
    0) log "no cluster to upgrade — the server will initialise a new one"; exit 0 ;;
    1) old=${old[0]} ;;
    *) die "more than one old cluster (${old[*]}) — refusing to guess which holds the data" ;;
esac

old_major=$(cat "$old/PG_VERSION")
old_bin="/usr/lib/postgresql/$old_major/bin"
new_bin="/usr/lib/postgresql/$PG_MAJOR/bin"
[ -x "$old_bin/postgres" ] || die "$old is Postgres $old_major, but this image carries no $old_major binaries"
# pg_upgrade refuses a cluster that was not shut down cleanly, and starting
# it here to recover would be a second postmaster on the same files should the
# old server still be running in another container.
[ ! -e "$old/postmaster.pid" ] || die "$old has a postmaster.pid — the old server is running or did not shut down cleanly; stop it (docker compose stop postgres) and retry"

log "upgrading Postgres $old_major ($old) → $PG_MAJOR ($new)"
started=$(date +%s)

mkdir -p "$root/$PG_MAJOR"
rm -rf "$stage"
chown postgres:postgres "$root/$PG_MAJOR"
chown -R postgres:postgres "$old"
chmod 700 "$old"

# The new cluster must match what pg_upgrade copies into it; the one setting
# that differs by default is checksums, which initdb turns on from 18.
init_args=()
if "$old_bin/pg_controldata" "$old" | grep -q '^Data page checksum version: *0$'; then
    init_args+=(--no-data-checksums)
fi
gosu postgres "$new_bin/initdb" -D "$stage" -U "$user" --auth=trust "${init_args[@]}" >/dev/null

cd /tmp
gosu postgres "$new_bin/pg_upgrade" -b "$old_bin" -B "$new_bin" -d "$old" -D "$stage" \
    -U "$user" --copy -s /tmp 2>&1 | quiet_collation

# pg_upgrade carries the data, not the server's own config files — and the
# trust the new cluster was initialised with must not outlive this step.
for f in pg_hba.conf pg_ident.conf postgresql.auto.conf; do
    install -o postgres -g postgres -m 600 "$old/$f" "$stage/$f"
done

gosu postgres "$new_bin/pg_ctl" -D "$stage" -w -l /tmp/postgres-upgrade.log \
    -o "-c listen_addresses='' -c unix_socket_directories=/tmp" start >/dev/null
q() { gosu postgres "$new_bin/psql" -h /tmp -U "$user" -v ON_ERROR_STOP=1 -X -Atq "$@"; }

# One session per database, so each warns about its collation only once.
for db in $(q -d postgres -c "SELECT datname FROM pg_database WHERE datallowconn" 2> >(quiet_collation >&2)); do
    # The new image may sit on a newer C library than the old one, and a text
    # index sorted under the old library's rules can then return wrong results.
    # Rebuild the indexes that sort by a C-library collation before telling
    # Postgres the database is current.
    if ! out=$(q -d "$db" 2>&1 <<'SQL'
DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT x.extname FROM pg_extension x JOIN pg_available_extensions a ON a.name = x.extname
           WHERE x.extversion <> a.default_version LOOP
    EXECUTE format('ALTER EXTENSION %I UPDATE', r.extname);
    RAISE NOTICE 'extension % updated', r.extname;
  END LOOP;
  IF (SELECT datcollversion IS DISTINCT FROM pg_database_collation_actual_version(oid)
      FROM pg_database WHERE datname = current_database()) THEN
    FOR r IN SELECT DISTINCT i.indexrelid::regclass AS idx
             FROM (SELECT indexrelid, unnest(indcollation) AS coll FROM pg_index) i
             JOIN pg_collation c ON c.oid = i.coll
             WHERE c.collprovider IN ('d', 'c') AND c.collname NOT IN ('C', 'POSIX') LOOP
      EXECUTE format('REINDEX INDEX %s', r.idx);
      RAISE NOTICE 'reindexed %', r.idx;
    END LOOP;
    EXECUTE format('ALTER DATABASE %I REFRESH COLLATION VERSION', current_database());
  END IF;
END $$;
SQL
    ); then
        printf '%s\n' "$out" >&2
        die "post-upgrade step failed in database $db"
    fi
    printf '%s\n' "$out" | quiet_collation
done
# pg_upgrade carries the planner statistics over from 18 on; only what it
# cannot carry is gathered here.
gosu postgres "$new_bin/vacuumdb" -h /tmp -U "$user" --all --analyze-only --missing-stats-only -q

gosu postgres "$new_bin/pg_ctl" -D "$stage" -w -m fast stop >/dev/null
mv "$stage" "$new"

log "done in $(( $(date +%s) - started )) s — the Postgres $old_major cluster is kept, still usable, at $old"
