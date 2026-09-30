"""Embed every reference photo and every CCTV face sample with each candidate
model, on identical pixels. Probes are BABA's stored aligned 112x112 crops;
references are re-detected with SCRFD-10G and aligned by BABA's own code, so
every model sees exactly the input the production pipeline would give it."""

import csv
import json
import logging
import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, "/work/shim")
sys.path.insert(0, "/work/facemoe")

from baba_core.face import ArcFaceStyleEmbedder, SCRFDDetector, align_face
from baba_core.face_models import PREPROC_ARCFACE_CLASSIC, PREPROC_AURAFACE

DATA = Path("/work/data")
MODELS = Path("/work/models")
OUT = Path("/work/out")
OUT.mkdir(exist_ok=True)


def read_rgb(path: Path) -> np.ndarray | None:
    bgr = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if bgr is None:
        return None
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)


# ---------------------------------------------------------------- inputs
probes = []
with open(DATA / "samples.csv") as f:
    for r in csv.DictReader(f):
        img = read_rgb(DATA / r["face_crop_path"])
        if img is None or img.shape[:2] != (112, 112):
            continue
        probes.append((r, img))
print(f"probes: {len(probes)}")

detector = SCRFDDetector(MODELS / "face_scrfd_10g.onnx", name="scrfd_10g")
refs = []
skipped = 0
with open(DATA / "refs.csv") as f:
    for r in csv.DictReader(f):
        img = read_rgb(DATA / r["photo_path"])
        if img is None:
            skipped += 1
            continue
        det = detector.detect(img)
        if det is None:
            skipped += 1
            continue
        refs.append((r, align_face(img, det.landmarks)))
print(f"refs: {len(refs)} aligned, {skipped} skipped")

probe_imgs = np.stack([p[1] for p in probes])
ref_imgs = np.stack([r[1] for r in refs])
np.save(OUT / "probe_imgs.npy", probe_imgs)
np.save(OUT / "ref_imgs.npy", ref_imgs)
with open(OUT / "probes.json", "w") as f:
    json.dump([p[0] for p in probes], f)
with open(OUT / "refs.json", "w") as f:
    json.dump([r[0] for r in refs], f)


def l2(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v, axis=1, keepdims=True)
    return v / np.maximum(n, 1e-9)


# ---------------------------------------------------------------- embedders
def fuse_flip(f1: np.ndarray, f2: np.ndarray) -> np.ndarray:
    """Norm-weighted average of the embedding of an image and of its mirror,
    the test-time trick the FaceMoE evaluation uses; the norm is a quality
    proxy, so the better-looking side of the face weighs more."""
    n1 = np.linalg.norm(f1, axis=1, keepdims=True)
    n2 = np.linalg.norm(f2, axis=1, keepdims=True)
    return (f1 / n1) * (n1 / (n1 + n2)) + (f2 / n2) * (n2 / (n1 + n2))


def run_baba(name: str, filename: str, preprocess: str, flip: bool = False):
    emb = ArcFaceStyleEmbedder(MODELS / filename, preprocess, name=name)
    t = time.time()
    imgs = np.concatenate([ref_imgs, probe_imgs])
    out = np.stack([emb.embed(x) for x in imgs])
    if flip:
        out = fuse_flip(out, np.stack([emb.embed(x[:, ::-1]) for x in imgs]))
    print(f"{name}: {len(out)} in {time.time() - t:.1f}s")
    return l2(out)


def run_lvface(name: str, filename: str, flip: bool = False):
    import onnxruntime as ort

    s = ort.InferenceSession(str(MODELS / filename), providers=["CPUExecutionProvider"])
    inp = s.get_inputs()[0].name

    def one(x):
        a = ((x.astype(np.float32) / 255.0) - 0.5) / 0.5
        a = np.ascontiguousarray(a.transpose(2, 0, 1)[None])
        return s.run(None, {inp: a})[0][0]

    t = time.time()
    imgs = np.concatenate([ref_imgs, probe_imgs])
    out = np.stack([one(x) for x in imgs])
    if flip:
        out = fuse_flip(out, np.stack([one(x[:, ::-1]) for x in imgs]))
    print(f"{name}: {len(out)} in {time.time() - t:.1f}s")
    return l2(out)


def run_facemoe(name: str, filename: str):
    import torch
    from backbones import get_model

    model = get_model("swin_moe", dropout=0.0, fp16=False, num_features=512, num_experts=3, k=2)
    sd = torch.load(str(MODELS / filename), map_location="cpu", weights_only=False)
    if isinstance(sd, dict) and "state_dict" in sd:
        sd = sd["state_dict"]
    sd = {k[7:] if k.startswith("module.") else k: v for k, v in sd.items()}
    missing, unexpected = model.load_state_dict(sd, strict=False)
    print(f"{name}: missing={len(missing)} unexpected={len(unexpected)}")
    if missing:
        print("  missing e.g.", missing[:5])
    if unexpected:
        print("  unexpected e.g.", unexpected[:5])
    model.eval()
    t = time.time()
    vecs = []
    imgs = np.concatenate([ref_imgs, probe_imgs])
    with torch.no_grad():
        for i in range(0, len(imgs), 16):
            batch = []
            for x in imgs[i : i + 16]:
                a = cv2.resize(x, (120, 120), interpolation=cv2.INTER_CUBIC)
                a = ((a.astype(np.float32) / 255.0) - 0.5) / 0.5
                batch.append(a.transpose(2, 0, 1))
            xb = torch.from_numpy(np.stack(batch))
            f1 = model(xb)
            f2 = model(torch.flip(xb, dims=[3]))
            n1 = f1.norm(dim=1, keepdim=True)
            n2 = f2.norm(dim=1, keepdim=True)
            w1, w2 = n1 / (n1 + n2), n2 / (n1 + n2)
            fused = (f1 / n1) * w1 + (f2 / n2) * w2
            vecs.append(fused.numpy())
    out = np.concatenate(vecs)
    print(f"{name}: {len(out)} in {time.time() - t:.1f}s")
    return l2(out)


which = sys.argv[1:] or ["topofr", "auraface", "lvface_b", "facemoe_tinyface", "facemoe_briar"]
for m in which:
    try:
        if m == "topofr":
            e = run_baba(m, "face_topofr_r200_glint360k.onnx", PREPROC_ARCFACE_CLASSIC)
        elif m == "auraface":
            e = run_baba(m, "face_auraface.onnx", PREPROC_AURAFACE)
        elif m == "lvface_b":
            e = run_lvface(m, "LVFace-B_Glint360K.onnx")
        elif m == "lvface_b_flip":
            e = run_lvface(m, "LVFace-B_Glint360K.onnx", flip=True)
        elif m == "lvface_l":
            e = run_lvface(m, "LVFace-L_Glint360K.onnx")
        elif m == "lvface_l_flip":
            e = run_lvface(m, "LVFace-L_Glint360K.onnx", flip=True)
        elif m == "topofr_flip":
            e = run_baba(m, "face_topofr_r200_glint360k.onnx", PREPROC_ARCFACE_CLASSIC, flip=True)
        elif m == "facemoe_tinyface":
            e = run_facemoe(m, "facemoe_tinyface.pt")
        elif m == "facemoe_briar":
            e = run_facemoe(m, "facemoe_briar.pt")
        else:
            raise SystemExit(f"unknown model {m}")
        np.save(OUT / f"emb_{m}.npy", e)
    except Exception:  # report and carry on with the others
        logging.exception("%s: FAILED", m)
