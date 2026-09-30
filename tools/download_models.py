"""Fetch default BABA models from upstream into BABA_MODELS_HOST.

Run via tools/download_models.sh (uses a throwaway python:3.14-slim container
so the host stays clean).

Downloads the full DETR-family detector zoo + DINOv2 embedder, all
Apache-2.0 / commercial-friendly. The active default is selected via
BABA_DETECTOR_MODEL; the frontend will (later) let the user switch
without re-downloading.

This script intentionally does NOT pull Ultralytics YOLO — those are
AGPL-3.0 and a commercial-use risk; user-supplied only.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("baba.tools.download")


@dataclass(slots=True)
class ModelSpec:
    target_name: str
    repo: str
    onnx_file: str = "onnx/model.onnx"
    sidecars: list[str] = field(default_factory=lambda: ["config.json", "preprocessor_config.json"])
    # Optional: an HTTP URL to fetch directly (bypasses huggingface_hub).
    # Useful for repos where the file isn't under the standard ONNX layout.
    direct_url: str | None = None
    # Optional: pin to a specific commit SHA / tag. Content at a commit SHA is
    # immutable on the Hub, so pinning makes a clean install byte-reproducible
    # and immune to an upstream force-push. Set it for any third-party (non-org)
    # repo we depend on. None = track the repo's default branch.
    revision: str | None = None


# SAM2 — Segment Anything 2 (Apache 2.0). Used by the api service to
# refine VLM-suggested zone polygons into pixel-perfect masks. Standard
# Meta-style encoder/decoder split.
SAM2_MODELS: list[ModelSpec] = [
    ModelSpec(
        target_name="sam2-hiera-tiny-encoder.onnx",
        repo="vietanhdev/segment-anything-2-onnx-models",
        onnx_file="sam2_hiera_tiny.encoder.onnx",
        sidecars=[],
    ),
    ModelSpec(
        target_name="sam2-hiera-tiny-decoder.onnx",
        repo="vietanhdev/segment-anything-2-onnx-models",
        onnx_file="sam2_hiera_tiny.decoder.onnx",
        sidecars=[],
    ),
]


# Detectors — DETR-family, all Apache 2.0 upstream, NMS-free.
# All ship as multiple precisions (model.onnx, model_fp16.onnx, model_int8.onnx)
# in the same HF repo; we mirror FP32 by default — backends quantize at load.
DETECTORS: list[ModelSpec] = [
    # RT-DETRv2 — most-downloaded DETR family on HF. Default for BABA.
    ModelSpec("rtdetrv2-r18.onnx", "onnx-community/rtdetr_v2_r18vd-ONNX"),
    ModelSpec("rtdetrv2-r34.onnx", "onnx-community/rtdetr_v2_r34vd-ONNX"),
    ModelSpec("rtdetrv2-r50.onnx", "onnx-community/rtdetr_v2_r50vd-ONNX"),
    ModelSpec("rtdetrv2-r101.onnx", "onnx-community/rtdetr_v2_r101vd-ONNX"),
    # D-FINE COCO — newer, smaller, faster at same mAP than RT-DETR for n/s sizes
    ModelSpec("d-fine-n.onnx", "onnx-community/dfine_n_coco-ONNX"),
    ModelSpec("d-fine-s.onnx", "onnx-community/dfine_s_coco-ONNX"),
    ModelSpec("d-fine-m.onnx", "onnx-community/dfine_m_coco-ONNX"),
    ModelSpec("d-fine-l.onnx", "onnx-community/dfine_l_coco-ONNX"),
    ModelSpec("d-fine-x.onnx", "onnx-community/dfine_x_coco-ONNX"),
]

# RF-DETR (Roboflow / LWDETR head, DINOv2 backbone) — Apache 2.0 up to Large.
# Heavier ViT backbone: cleaner false-positive profile but memory-bandwidth-
# bound, so it belongs on a DISCRETE GPU (Arc/dGPU), NOT a budget iGPU
# (measured ~3 fps + ~2 GB OOM on an N305 UHD vs ~24 fps on an Arc A380).
# install.sh's detect_gpu_tier picks it only for the dgpu-arc tier. I/O verified
# structurally against our validated 640 export: dynamic [B,3,H,W] input (set to
# 640, must be divisible by 32), outputs pred_boxes[.,300,4] then logits[.,300,91]
# — a positional drop-in for RFDetrPostprocessor.
RFDETR_MODELS: list[ModelSpec] = [
    ModelSpec(
        target_name="rf-detr-nano.onnx",
        repo="onnx-community/rfdetr_nano-ONNX",
        onnx_file="onnx/model.onnx",
        sidecars=[],
        revision="eae21cee0687a91bcf9fa071605c48d7705d2d91",
    ),
]

# Embedders — vision backbones for re-ID, all Apache 2.0
EMBEDDERS: list[ModelSpec] = [
    ModelSpec("dinov2-vits14.onnx", "onnx-community/dinov2-small"),
    ModelSpec("dinov2-vitb14.onnx", "onnx-community/dinov2-base"),
]

# Appearance ReID — OSNet x0.25 (msmt17), MIT (KaiyangZhou/torchreid). Used by
# BOTH the tracker (inline appearance ReID) and the embedder (body backbone) —
# they share /models/osnet_x0_25_msmt17.onnx, so this is a hard requirement on
# the require-GPU nvidia/intel variants (both services refuse to start without
# it). ~0.9 MB, 512-d output (matches migration 040's vector(512)). No permissive
# ORG mirror exists; anriha's repo is the cleanest MIT-licensed ONNX export, so
# we pin its commit SHA for byte-reproducibility. Mirror into a Bacinac HF repo
# if upstream-deletion resilience is ever needed.
REID_MODELS: list[ModelSpec] = [
    ModelSpec(
        target_name="osnet_x0_25_msmt17.onnx",
        repo="anriha/osnet_x0_25_msmt17",
        onnx_file="osnet_x0_25_msmt17.onnx",
        sidecars=[],
        revision="1e22b925c70caa5591e9fab3f5540af8484bcad8",
    ),
]

# Face stack — only commercially clean choice that ships with explicit
# Apache 2.0 / MIT weights (most InsightFace variants are research-only
# due to MS1M/Glint training data). See core/src/baba_core/face.py for
# the license audit notes.
FACE_MODELS: list[ModelSpec] = [
    # YuNet — face detector (~1 MB, MIT). opencv_zoo's HF mirror.
    ModelSpec(
        target_name="face_yunet.onnx",
        repo="opencv/face_detection_yunet",
        onnx_file="face_detection_yunet_2023mar.onnx",
        sidecars=[],
    ),
    # AuraFace v1 — ArcFace ResNet100 retrained on commercial-clean data
    # by fal (~261 MB, Apache 2.0). The only widely-distributed face
    # embedder with explicit Apache 2.0 on the weights.
    ModelSpec(
        target_name="face_auraface.onnx",
        repo="fal/AuraFace-v1",
        onnx_file="glintr100.onnx",
        sidecars=[],
    ),
]


def fetch(target_dir: Path, spec: ModelSpec) -> bool:
    from huggingface_hub import hf_hub_download
    from huggingface_hub.utils import EntryNotFoundError, RepositoryNotFoundError

    dest = target_dir / spec.target_name
    if dest.exists():
        log.info("skip %s (already present, %d MB)", spec.target_name, dest.stat().st_size // 1_000_000)
    else:
        log.info("downloading %s from %s:%s", spec.target_name, spec.repo, spec.onnx_file)
        try:
            src = hf_hub_download(repo_id=spec.repo, filename=spec.onnx_file, revision=spec.revision)
        except (EntryNotFoundError, RepositoryNotFoundError) as exc:
            log.error("failed to download %s: %s", spec.target_name, exc)
            return False
        shutil.copyfile(src, dest)
        log.info("wrote %s (%d MB)", dest, dest.stat().st_size // 1_000_000)

    stem = dest.stem
    for sidecar in spec.sidecars:
        sidecar_dest = target_dir / f"{stem}.{Path(sidecar).name}"
        if sidecar_dest.exists():
            continue
        try:
            src = hf_hub_download(repo_id=spec.repo, filename=sidecar)
            shutil.copyfile(src, sidecar_dest)
        except (EntryNotFoundError, RepositoryNotFoundError):
            log.warning("sidecar %s missing in %s — skipping", sidecar, spec.repo)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--models-dir",
        type=Path,
        default=Path("/models"),
        help="Target directory (default /models — intended as a volume mount)",
    )
    parser.add_argument(
        "--detectors-only", action="store_true",
        help="Fetch only detectors (skip embedders and face stack)",
    )
    parser.add_argument(
        "--embedders-only", action="store_true",
        help="Fetch only DINOv2 embedders (skip detectors and face stack)",
    )
    parser.add_argument(
        "--face-only", action="store_true",
        help="Fetch only the face stack (YuNet + AuraFace)",
    )
    parser.add_argument(
        "--no-face", action="store_true",
        help="Skip the face stack (face-aware re-ID will be disabled at runtime)",
    )
    parser.add_argument(
        "--sam2-only", action="store_true",
        help="Fetch only the SAM2 zone-refinement models",
    )
    parser.add_argument(
        "--no-sam2", action="store_true",
        help="Skip the SAM2 stack (zone suggestions will not be pixel-refined)",
    )
    parser.add_argument(
        "--minimal", action="store_true",
        help=(
            "Fetch only the BABA default set (RT-DETRv2-R18 detector, "
            "OSNet-x0.25 appearance ReID, DINOv2-S body embedder, full face "
            "stack). ~430 MB vs the full zoo's ~3 GB. install.sh uses this by default."
        ),
    )
    parser.add_argument(
        "--detector",
        default="rtdetrv2-r18",
        help=(
            "Which detector the --minimal set fetches (basename, no .onnx). "
            "install.sh passes the GPU-tier default here: rf-detr-nano (Arc), "
            "d-fine-s (iGPU/nvidia), rtdetrv2-r18 (cpu). Ignored for the full zoo."
        ),
    )
    parser.add_argument(
        "--bake-nv12", action="store_true",
        help=(
            "After download, bake a .nv12.onnx variant of every fetched "
            "detector (YUV->RGB + normalise + permute in-graph) via "
            "tools/bake_preproc.py. Pass on nvidia/intel installs whose "
            "decoder chain emits NV12; pointless on cpu (RGB decode). "
            "Needs the onnx package (download_models.sh installs it)."
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args.models_dir.mkdir(parents=True, exist_ok=True)

    # Selector logic. --minimal short-circuits with just the defaults
    # the BABA stack ships pointing at. Otherwise: any --x-only flag
    # picks exactly that bucket; without one we fetch every default
    # bucket unless --no-face explicitly excludes the face stack.
    specs: list[ModelSpec] = []
    if args.minimal:
        # The GPU-tier detector install.sh chose (--detector), plus the shared
        # body/face/reid/sam defaults. Falls back to rtdetrv2-r18 if the name
        # isn't known so a typo can't leave the install with no detector.
        want = f"{args.detector}.onnx"
        det = next((s for s in (DETECTORS + RFDETR_MODELS) if s.target_name == want), None)
        if det is None:
            log.warning("unknown --detector %r; falling back to rtdetrv2-r18", args.detector)
            det = next(s for s in DETECTORS if s.target_name == "rtdetrv2-r18.onnx")
        specs.append(det)
        specs.extend(s for s in EMBEDDERS if s.target_name == "dinov2-vits14.onnx")
        specs.extend(REID_MODELS)  # osnet — hard-required by tracker + embedder
        if not args.no_face:
            specs.extend(FACE_MODELS)
        if not args.no_sam2:
            specs.extend(SAM2_MODELS)
    else:
        only_flags = [
            args.detectors_only, args.embedders_only,
            args.face_only, args.sam2_only,
        ]
        explicit_only = any(only_flags)
        if args.detectors_only or not explicit_only:
            specs.extend(DETECTORS)
        if args.embedders_only or not explicit_only:
            specs.extend(EMBEDDERS)
            specs.extend(REID_MODELS)
        if args.face_only or (not explicit_only and not args.no_face):
            specs.extend(FACE_MODELS)
        if args.sam2_only or (not explicit_only and not args.no_sam2):
            specs.extend(SAM2_MODELS)

    failures = sum(0 if fetch(args.models_dir, s) else 1 for s in specs)

    if args.bake_nv12:
        bake_script = Path(__file__).with_name("bake_preproc.py")
        detector_names = sorted({s.target_name for s in DETECTORS})
        for name in detector_names:
            src = args.models_dir / name
            if not src.exists():
                continue
            dst = args.models_dir / f"{src.stem}.nv12.onnx"
            if dst.exists():
                log.info("skip bake %s (already present)", dst.name)
                continue
            log.info("baking %s -> %s (NV12 preproc in-graph)", name, dst.name)
            result = subprocess.run(
                [sys.executable, str(bake_script),
                 "--input", str(src), "--output", str(dst),
                 "--input-format", "nv12"],
                check=False,
            )
            if result.returncode != 0:
                log.error("bake failed for %s", name)
                failures += 1

    if failures:
        log.error("%d model step(s) failed", failures)
        return 1
    log.info("all models present in %s", args.models_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
