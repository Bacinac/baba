"""Face recognition model registry.

BABA ships AuraFace v1 (Apache 2.0 on both code AND weights) as the
default, commercially-clean face recognition model. Users who want
higher accuracy on hard scenes (wide-angle / distance / pose variance)
can opt in to research-grade models like TopoFR by downloading the
weights themselves and pointing BABA at the file via
`BABA_FACE_RECOGNITION_MODEL`.

Critically: BABA never redistributes opt-in model weights. Users obtain
them directly from the source repository on their own legal terms.
This is the same "Bring Your Own Model" pattern that ffmpeg, VLC,
GStreamer and similar OSS projects use for codecs/plugins whose
upstream licensing is not redistribution-friendly.

When swapping the active model:
  - Embedding spaces between models are NOT comparable. AuraFace's
    512-d vector and TopoFR R200's 512-d vector encode the same face
    differently; cross-comparing them returns noise.
  - Reference embeddings stored in `identity_labels.face_embedding` and
    samples in `track_embedding_samples.face_embedding` must be
    recomputed with the active model before matching is meaningful.
    See services/embedder/scripts/recompute_face_embeddings.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Preprocessing variants — string keys mapped to (subtract, divide) for the
# ArcFace-style `(pixel - subtract) / divide` normalisation. Both models we
# support today use uint8 → [-1, 1] roughly, but the exact divisor differs
# by ~1% between AuraFace (128.0) and the InsightFace-derived TopoFR (127.5).
# Wrong divisor causes a small accuracy drop, not a hard failure, but we keep
# them distinct so future models with materially different prep (e.g.
# ImageNet mean/std) plug in cleanly.
PREPROC_AURAFACE = "arcface_128"  # (x - 127.5) / 128.0
PREPROC_ARCFACE_CLASSIC = "arcface_127_5"  # (x - 127.5) / 127.5


@dataclass(slots=True, frozen=True)
class FaceModelSpec:
    key: str  # short id used in env vars / DB / UI selector
    name: str  # human-readable
    description: str  # what this model is, license posture
    license: str  # spdx-style or "unspecified"
    tier: str  # "default" | "byom" | "commercial"
    model_filename: str  # filename under /models/ — relative
    input_size: int  # spatial dim, 112 for ArcFace family
    embedding_dim: int  # output vector size, must match DB schema
    preprocess: str  # PREPROC_* constant
    # If True, BABA distribution includes the weights in the image. If False,
    # the user must place the model file at `model_filename` themselves.
    # Used by the UI selector to flag opt-in models with a download notice.
    bundled_with_baba: bool


FACE_MODELS: dict[str, FaceModelSpec] = {
    "auraface": FaceModelSpec(
        key="auraface",
        name="AuraFace v1",
        description=(
            "ResNet100 + ArcFace head, trained on commercial-clean data "
            "by fal.ai. The default for BABA distributions because both "
            "code AND weights are Apache 2.0 — it is the only permissive "
            "face embedder that exists; every research-grade alternative "
            "(TopoFR, LVFace, AdaFace, EdgeFace) trains on MS1MV2 / "
            "Glint360K / WebFace and is non-commercial. "
            "⚠ MEASURED 2026-07-15 on a real deployment: on CLEAN 1440px "
            "portraits, two photos of the SAME person sit a median 0.345 "
            "apart and 31% of pairs exceed the 0.40 match threshold — it "
            "barely separates a person from themselves in studio "
            "conditions. On surveillance faces it produced no identity "
            "signal at all (same-person median 0.870 vs 0.886 for random "
            "pairs). It publishes no IJB-C or TinyFace numbers; the LFW / "
            "CFP-FP scores in its card are saturated frontal benchmarks "
            "that say nothing about a camera. Fine for cooperative, "
            "close, frontal capture. For surveillance re-ID, expect it "
            "not to work, and know that the fix is BYOM (see TopoFR) at "
            "the cost of the licence."
        ),
        license="apache-2.0",
        tier="default",
        model_filename="face_auraface.onnx",
        input_size=112,
        embedding_dim=512,
        preprocess=PREPROC_AURAFACE,
        bundled_with_baba=True,
    ),
    "topofr_r200_glint360k": FaceModelSpec(
        key="topofr_r200_glint360k",
        name="TopoFR R200 (Glint360K)",
        description=(
            "ResNet200 + ArcFace + topology alignment loss, NeurIPS 2024. "
            "Best-in-class accuracy on IJB-C (~97.84% @ FAR 1e-4). "
            "⚠ BYOM: weights have no explicit license — the GitHub repo "
            "at github.com/DanJun6737/TopoFR lacks a LICENSE file. "
            "Personal/development use is uncontested but commercial "
            "redistribution is not permitted without explicit author "
            "grant. Download the ONNX from the HuggingFace Space at "
            "huggingface.co/spaces/developer0hye/TopoFR-Face-Recognition "
            "and place at /models/{model_filename}."
        ),
        license="unspecified",
        tier="byom",
        model_filename="face_topofr_r200_glint360k.onnx",
        input_size=112,
        embedding_dim=512,
        preprocess=PREPROC_ARCFACE_CLASSIC,
        bundled_with_baba=False,
    ),
}


DEFAULT_FACE_MODEL = "auraface"


def get_face_model(key: str) -> FaceModelSpec:
    """Look up a face model by its registry key. Raises KeyError with a
    helpful message listing available keys when the key isn't known."""
    try:
        return FACE_MODELS[key]
    except KeyError:
        raise KeyError(
            f"Unknown face recognition model {key!r}. "
            f"Registered: {sorted(FACE_MODELS.keys())}. "
            f"Set BABA_FACE_RECOGNITION_MODEL to one of these or extend "
            f"core/src/baba_core/face_models.py to add another."
        ) from None


def resolve_model_path(spec: FaceModelSpec, models_dir: Path) -> Path:
    """Compose the full filesystem path for a model spec given the
    runtime /models mount root."""
    return models_dir / spec.model_filename
