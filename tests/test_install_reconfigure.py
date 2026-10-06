"""install.sh on an existing .env: a configuration flag is applied or refused, never dropped.

Measured 2026-09-22 on a fresh LXC: the first run chose --variant=intel on a
host without a GPU and failed loudly on /dev/dri. The second run said
--variant=cpu, but an existing .env sent the non-interactive installer down
the upgrade path, which keeps every value — BABA_VARIANT=intel and the intel
compose override came back, and so did the same failure.

The installer runs for real in a copy of the tree, against stub docker/sudo
binaries, so what is checked is the .env it leaves behind.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

INTEL_ENV = """\
BABA_MODELS_HOST={d}/models
BABA_STATE_HOST={d}/state
BABA_MEDIA_HOST={d}/media
BABA_UID=1000
BABA_GID=1000
BABA_VARIANT=intel
COMPOSE_FILE=docker-compose.yml:docker-compose.intel.yml
BABA_DECODER_BACKEND=
BABA_INTEL_RENDER_GID=993
BABA_DETECTOR_MODEL=/models/d-fine-s.nv12.onnx
BABA_DETECTOR_MODEL_FAMILY=dfine
BABA_DETECTOR_OV_PRECISION=
BABA_TRACKER_FPS=7
POSTGRES_USER=baba
POSTGRES_PASSWORD=pg-secret
POSTGRES_DB=baba
NATS_USER=baba
NATS_PASSWORD=nats-secret
BABA_PEER_NATS_PASSWORD=peer-secret
BABA_GO2RTC_API_USER=baba
BABA_GO2RTC_API_PASSWORD=g2-secret
BABA_API_PORT=8080
BABA_ADMIN_USERNAME=marko
BABA_ADMIN_PASSWORD=
BABA_SECRET_KEY=jwt-secret
BABA_COOKIE_SECURE=false
BABA_CORS_ORIGINS=
"""

DOCKER_STUB = """\
#!/bin/sh
printf '%s\n' "$*" >> "$PWD/docker-calls"
case "$*" in
    *inspect*) echo healthy;;
    *version*) echo 27.0.0;;
esac
exit 0
"""


# The same host as written before each backend followed the variant: a
# decoder chain, a detector chain, provider lists and the NV12 switch.
RETIRED = """\
# Decoder chain for ffmpeg/PyAV inside the ingestor.
BABA_DECODER_BACKENDS=qsv-nv12,vaapi-nv12,software

# =========================================================================
# Detector
# =========================================================================
BABA_DETECTOR_BACKENDS=tensorrt,openvino,onnxruntime
BABA_DETECTOR_GPU_NV12=1
# Inference EPs: OpenVINO, CPU nominal fallback.
BABA_INFERENCE_PROVIDERS=intel,cpu
BABA_API_INFERENCE_PROVIDERS=intel,cpu
"""


def _tree(tmp: Path, env: str = INTEL_ENV) -> Path:
    app = tmp / "baba"
    app.mkdir()
    shutil.copy2(ROOT / "install.sh", app / "install.sh")
    shutil.copytree(ROOT / "scripts", app / "scripts")
    shutil.copy2(ROOT / "go2rtc.example.yaml", app / "go2rtc.example.yaml")
    (app / ".env").write_text(env.format(d=tmp))
    stubs = tmp / "bin"
    stubs.mkdir()
    (stubs / "docker").write_text(DOCKER_STUB)
    (stubs / "sudo").write_text("#!/bin/sh\nexit 1\n")
    (stubs / "systemctl").write_text("#!/bin/sh\nexit 1\n")
    (stubs / "lspci").write_text("#!/bin/sh\nexit 0\n")
    for f in stubs.iterdir():
        f.chmod(0o755)
    return app


def _run(app: Path, *flags: str) -> subprocess.CompletedProcess:
    env = dict(os.environ,
               PATH=f"{app.parent / 'bin'}:{os.environ['PATH']}",
               GIT_CEILING_DIRECTORIES=str(app.parent))
    return subprocess.run(
        ["bash", str(app / "install.sh"), "--non-interactive", "--pull", "--models=skip", *flags],
        cwd=app, env=env, capture_output=True, text=True, timeout=120,
    )


def _env(app: Path) -> dict[str, str]:
    out = {}
    for line in (app / ".env").read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


def test_upgrade_provisions_the_configured_detector(tmp_path):
    app = _tree(tmp_path)
    tools = app / "tools"
    tools.mkdir()
    downloader = tools / "download_models.sh"
    downloader.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$@" > "$PWD/download-args"\n'
        'printf "download\\n" >> "$PWD/docker-calls"\n'
    )
    downloader.chmod(0o755)
    result = _run(app, "--upgrade", "--models=minimal")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (app / "download-args").read_text().splitlines() == [
        "--minimal", "--bake-nv12", "--detector", "d-fine-s",
    ]
    calls = (app / "docker-calls").read_text().splitlines()
    assert calls.index("download") < next(i for i, call in enumerate(calls) if "up -d" in call)


def test_a_different_variant_is_applied_to_the_existing_env(tmp_path):
    app = _tree(tmp_path)
    r = _run(app, "--variant=cpu", f"--media-host={tmp_path}/media", "--admin=marko")
    assert r.returncode == 0, r.stdout + r.stderr
    env = _env(app)
    assert env["BABA_VARIANT"] == "cpu"
    assert env["COMPOSE_FILE"] == "docker-compose.yml"
    assert env["BABA_DECODER_BACKEND"] == ""
    assert env["BABA_DETECTOR_MODEL"] == "/models/rtdetrv2-r18.onnx"
    assert env["BABA_SECRET_KEY"] == "jwt-secret"
    assert env["POSTGRES_PASSWORD"] == "pg-secret"
    assert env["BABA_TRACKER_FPS"] == "7"
    assert list(app.glob(".env.bak.*"))


def test_a_reconfigure_without_variant_keeps_the_configured_one(tmp_path):
    app = _tree(tmp_path)
    r = _run(app, "--hostname=baba.example.com")
    assert r.returncode == 0, r.stdout + r.stderr
    env = _env(app)
    assert env["BABA_VARIANT"] == "intel"
    assert env["BABA_DECODER_BACKEND"] == "qsv-nv12"  # lspci names no Arc: an iGPU
    assert env["BABA_DETECTOR_MODEL"] == "/models/d-fine-s.nv12.onnx"
    assert env["BABA_CORS_ORIGINS"] == "https://baba.example.com"
    assert env["BABA_COOKIE_SECURE"] == "true"


def test_upgrade_refuses_a_flag_that_would_change_the_env(tmp_path):
    app = _tree(tmp_path)
    before = (app / ".env").read_text()
    r = _run(app, "--upgrade", "--variant=cpu")
    assert r.returncode != 0
    assert "--variant=cpu differs from BABA_VARIANT=intel" in r.stdout + r.stderr
    assert (app / ".env").read_text() == before


def test_upgrade_accepts_flags_that_restate_the_env(tmp_path):
    app = _tree(tmp_path)
    r = _run(app, "--upgrade", "--variant=intel", "--admin=marko")
    assert r.returncode == 0, r.stdout + r.stderr
    assert _env(app)["BABA_VARIANT"] == "intel"


def test_upgrade_generates_a_missing_go2rtc_password_and_keeps_an_existing_one(tmp_path):
    (tmp_path / "kept").mkdir()
    (tmp_path / "bare").mkdir()
    kept = _tree(tmp_path / "kept")
    r = _run(kept, "--upgrade")
    assert r.returncode == 0, r.stdout + r.stderr
    assert _env(kept)["BABA_GO2RTC_API_PASSWORD"] == "g2-secret"

    bare = _tree(tmp_path / "bare")
    env = (bare / ".env").read_text().replace("BABA_GO2RTC_API_PASSWORD=g2-secret\n", "")
    (bare / ".env").write_text(env)
    r = _run(bare, "--upgrade")
    assert r.returncode == 0, r.stdout + r.stderr
    assert len(_env(bare)["BABA_GO2RTC_API_PASSWORD"]) >= 24


@pytest.mark.parametrize(
    ("cluster", "major"),
    [("postgresql/18/docker", "18"), ("postgres", "17")],
    ids=["current", "before-pg18"],
)
def test_a_new_admin_on_an_existing_database_is_refused(tmp_path, cluster, major):
    app = _tree(tmp_path)
    (tmp_path / "state" / cluster).mkdir(parents=True)
    (tmp_path / "state" / cluster / "PG_VERSION").write_text(f"{major}\n")
    before = (app / ".env").read_text()
    r = _run(app, "--admin=ops")
    assert r.returncode != 0
    assert "--admin=ops differs from BABA_ADMIN_USERNAME=marko" in r.stdout + r.stderr
    assert (app / ".env").read_text() == before


def _retired_env() -> str:
    return INTEL_ENV.replace("BABA_DECODER_BACKEND=\n", "") + RETIRED


@pytest.mark.parametrize("flags", [("--upgrade",), ("--hostname=baba.example.com",)],
                         ids=["upgrade", "reconfigure"])
def test_retired_keys_leave_and_the_chain_head_stays_the_decoder(tmp_path, flags):
    app = _tree(tmp_path, _retired_env())
    r = _run(app, *flags)
    assert r.returncode == 0, r.stdout + r.stderr
    text = (app / ".env").read_text()
    env = _env(app)
    assert env["BABA_DECODER_BACKEND"] == "qsv-nv12"
    for gone in ("BABA_DECODER_BACKENDS", "BABA_DETECTOR_BACKENDS", "BABA_DETECTOR_GPU_NV12",
                 "BABA_INFERENCE_PROVIDERS", "BABA_API_INFERENCE_PROVIDERS"):
        assert gone not in env
    assert "Decoder chain" not in text and "Inference EPs" not in text
    assert "# Detector\n" in text
    assert env["BABA_DETECTOR_MODEL"] == "/models/d-fine-s.nv12.onnx"
    assert list(app.glob(".env.bak.*"))


def test_a_chain_headed_by_a_deleted_decoder_leaves_the_variant_to_decide(tmp_path):
    env = _retired_env().replace("qsv-nv12,vaapi-nv12,software", "vaapi,software")
    app = _tree(tmp_path, env)
    r = _run(app, "--upgrade")
    assert r.returncode == 0, r.stdout + r.stderr
    assert _env(app)["BABA_DECODER_BACKEND"] == ""
    assert "first decoder no longer exists" in r.stdout + r.stderr


def test_a_chain_headed_by_the_variants_own_decoder_pins_nothing(tmp_path):
    env = _retired_env().replace("qsv-nv12,vaapi-nv12,software", "vaapi-nv12,qsv-nv12")
    app = _tree(tmp_path, env)
    r = _run(app, "--upgrade")
    assert r.returncode == 0, r.stdout + r.stderr
    assert _env(app)["BABA_DECODER_BACKEND"] == ""
    assert "BABA_DECODER_BACKENDS" not in _env(app)
