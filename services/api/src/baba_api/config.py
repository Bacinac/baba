from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from baba_core import dsn_from_env, go2rtc_auth_from_env


@dataclass(slots=True, frozen=True)
class ApiConfig:
    host: str
    port: int
    dsn: str
    nats_url: str
    migrations_dir: Path
    state_dir: Path
    media_path: Path
    go2rtc_url: str
    # Basic-auth credentials for go2rtc's REST API. go2rtc is
    # network_mode:host, so its :1984 API is exposed on every host interface —
    # without auth any LAN host can read every camera's plaintext RTSP
    # credentials via /api/config.
    go2rtc_auth: tuple[str, str]
    # DINOv2 ONNX path for reference-photo enrollment. Empty string → the
    # feature is disabled (api still starts; upload endpoint returns 503).
    embedder_model_path: str
    # SAM2 ONNX paths for zone-suggestion refinement. Both must be
    # present + readable to enable refinement; otherwise the VLM's
    # rough polygon is returned untouched (no error, just a log line).
    sam2_encoder_path: str
    sam2_decoder_path: str
    # Master switch — even if the model files are present, set this to
    # false to bypass SAM2 entirely (e.g. when debugging whether SAM2
    # or the VLM is responsible for a bad suggestion).
    sam2_refine_enabled: bool
    # Background loop that asks the configured AI provider to describe
    # newly observed identities the user hasn't labelled yet. Off by
    # default (costs money per identity, even at fractions of a cent).
    auto_describe_enabled: bool
    auto_describe_interval_s: int
    # Max identities to describe per tick — bounds the cost spike when a
    # batch of new identities pile up after a long offline period.
    auto_describe_batch: int
    # CORS origins allowed to call the API. Tuple of origin strings
    # (scheme + host + port). The literal "*" means "any origin" — note
    # that the cookie auth flow still requires the browser to send
    # credentials, which `*` + allow_credentials cannot do, so in
    # practice "*" only works for unauthenticated probes.
    cors_origins: tuple[str, ...]

    @classmethod
    def from_env(cls) -> ApiConfig:
        # POSTGRES_SSLMODE handling (empty = asyncpg default; `require` /
        # `verify-full` for an external TLS-only Postgres) lives in
        # baba_core.dsn_from_env, shared by every service.
        dsn = dsn_from_env()

        # CORS: env may be comma-separated. Empty falls back to the dev
        # SvelteKit origins so a fresh `docker compose up` Just Works
        # against the bundled web container. Production deployments
        # MUST set BABA_CORS_ORIGINS to the public origin(s).
        cors_raw = os.environ.get("BABA_CORS_ORIGINS", "").strip()
        if cors_raw:
            cors_origins = tuple(o.strip() for o in cors_raw.split(",") if o.strip())
        else:
            cors_origins = (
                "http://localhost:5173",
                "http://127.0.0.1:5173",
            )

        return cls(
            host=os.environ.get("BABA_API_HOST", "0.0.0.0"),
            port=int(os.environ.get("BABA_API_PORT", "8080")),
            dsn=dsn,
            nats_url=os.environ.get("BABA_NATS_URL", "nats://nats:4222"),
            migrations_dir=Path(os.environ.get("BABA_MIGRATIONS_DIR", "/app/db/migrations")),
            state_dir=Path(os.environ.get("BABA_STATE_PATH", "/state")),
            media_path=Path(os.environ.get("BABA_MEDIA_PATH", "/media")),
            go2rtc_url=os.environ.get("BABA_GO2RTC_URL", "http://host.docker.internal:1984"),
            go2rtc_auth=go2rtc_auth_from_env(),
            embedder_model_path=os.environ.get("BABA_EMBEDDER_MODEL", "").strip(),
            sam2_encoder_path=os.environ.get("BABA_SAM2_ENCODER_MODEL", "").strip(),
            sam2_decoder_path=os.environ.get("BABA_SAM2_DECODER_MODEL", "").strip(),
            sam2_refine_enabled=os.environ.get(
                "BABA_SAM2_REFINE",
                "1",
            ).lower()
            in ("1", "true", "yes"),
            auto_describe_enabled=os.environ.get("BABA_AI_AUTO_DESCRIBE", "0").lower()
            in ("1", "true", "yes"),
            auto_describe_interval_s=int(os.environ.get("BABA_AI_AUTO_DESCRIBE_INTERVAL_S", "120")),
            auto_describe_batch=int(os.environ.get("BABA_AI_AUTO_DESCRIBE_BATCH", "5")),
            cors_origins=cors_origins,
        )

