# Deployment

## Deploying to the live instances

Every BABA instance is listed in `deploy/hosts.conf` (git-ignored; start from
[`hosts.conf.example`](hosts.conf.example)), and [`deploy.sh`](deploy.sh) is the
only thing that deploys to them:

```bash
./deploy/deploy.sh --list          # what's registered
./deploy/deploy.sh cabin           # one instance
./deploy/deploy.sh home cabin      # several
./deploy/deploy.sh all             # everything
./deploy/deploy.sh --dry-run all   # plan only, change nothing
```

Adding an instance is a line in `hosts.conf` — never another script. That is
the point: the steps a deploy must get right (build the variant's base image
*before* the services, keep the storage tiers owned by uid 1000, wait on
container health instead of on `up` returning, prune afterwards) live in one
place, so no instance can drift from the others. Per-host quirks are declared
as flags in the inventory, not as forked copies of the procedure.

By default each host builds the images itself. `--pull` takes the published
ones instead — the right choice for a box too slow to build (compiling ffmpeg
on a 32-EU mini-PC is a twenty-minute affair). Publishing is the other half of
the same idea:

```bash
./deploy/publish.sh intel          # build + push this variant to the registry
./deploy/publish.sh --dry-run all  # plan only
```

`publish.sh` builds on the machine you run it from, on purpose: building every
variant in one place would drag the ~10 GB CUDA base onto hosts with no NVIDIA
GPU, and our dev box carries other projects that a multi-hour build would
starve. Build `intel`/`cpu` wherever is convenient and `nvidia` on the NVIDIA
box that already has the base cached — the images are plain amd64, so where
they were built doesn't matter to whoever pulls them.

This is also what keeps the promise on the landing page honest: `install.sh` on
a new machine pulls prebuilt images, and those images only track `main` if
someone publishes them after a change.

Source reaches a host one of two ways, set per host by the `sync` column:
`git` (the host pulls `main` into its own checkout) or `rsync` (this checkout
is mirrored over, for hosts without git credentials). Rsync deliberately never
carries `.env`, `models/`, `media/`, `state/`, `logs/` or the runtime
`go2rtc.yaml`: **code** comes from here, **config and data** belong to the
instance.

Hot-patching a file into a running container is not a deploy. `docker compose
up --force-recreate` (and anything that recreates a dependency, such as
rebuilding `web`) replaces the container from its image and silently discards
the patch — including patches you applied minutes earlier. Deploy instead.

## Reverse-proxy templates

Reference configs for production-style deployments of BABA. The default
`docker-compose.yml` ships with a dev SvelteKit + plain HTTP API. To
expose BABA on a public hostname you put a TLS-terminating reverse
proxy in front of it.

Two templates are provided:

- **caddy/** — drop-in [Caddy](https://caddyserver.com/) reverse proxy
  with automatic Let's Encrypt certs. Simplest path; one Caddyfile and
  a tiny compose override.
- **traefik/** — [Traefik v3](https://doc.traefik.io/traefik/) example
  for operators already running Traefik for other services. Slightly
  more verbose but routes through Docker labels so config stays with
  the service it routes to.

## Pre-flight (both templates)

1. A DNS record for the BABA hostname pointing at the docker host.
2. Ports 80 + 443 reachable from the public internet (Let's Encrypt
   HTTP-01 challenge) — or pre-existing certs if you bring your own.
3. In `.env`:
   - Set `BABA_COOKIE_SECURE=true` so session cookies aren't dropped
     by the browser on the now-HTTPS origin.
   - Set `BABA_CORS_ORIGINS=https://<your-host>` so the API accepts
     SvelteKit requests from the public origin.

## Caddy

```bash
# 1. Edit deploy/caddy/Caddyfile, replace `baba.example.com` with your host
# 2. Bring up the stack with the override applied:
docker compose -f docker-compose.yml -f deploy/caddy/docker-compose.caddy.yml up -d
```

Caddy will pull a cert from Let's Encrypt on first request and renew
automatically. The override removes the host port bind on `api` and `web`
so the only public-facing service is Caddy itself.

## Traefik

```bash
# 1. Edit deploy/traefik/traefik.yml — set your acme email
# 2. Edit deploy/traefik/docker-compose.traefik.yml labels — set Host(`...`)
docker compose \
  -f docker-compose.yml \
  -f deploy/traefik/docker-compose.traefik.yml up -d
```

The override mounts the Docker socket read-only (so Traefik can watch
labels) and persists the ACME state to `${BABA_STATE_HOST}/traefik`.

## What you do NOT get from these templates

- **Brute-force protection beyond the built-in `/auth/login` token
  bucket** — Caddy / Traefik can add fail2ban-style banning if needed,
  but it's out of scope here.
- **Multi-instance scaling** — both templates assume one api process.
  Horizontal scaling needs sticky session affinity or a shared session
  store, neither of which BABA implements yet.
- **Postgres / NATS exposure** — both stay loopback-only; if you need
  external access run a separate proxy or VPN.
