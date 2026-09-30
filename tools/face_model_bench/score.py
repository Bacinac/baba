"""Score every model the way BABA matches: a probe is compared to each
identity by the MINIMUM cosine distance over that identity's references.
Genuine = distance to the probe's own identity; impostor = the nearest OTHER
identity. Reported per model and per face-size band."""

import json
import sys
from pathlib import Path

import numpy as np

OUT = Path("/work/out")
probes = json.loads((OUT / "probes.json").read_text())
refs = json.loads((OUT / "refs.json").read_text())
n_ref = len(refs)
people = sorted({r["person"] for r in refs})
ref_person = np.array([r["person"] for r in refs])
probe_person = np.array([p["person"] for p in probes])
probe_px = np.array([float(p["face_px"]) for p in probes])
probe_fv = np.array([p["face_verified"] == "t" for p in probes])
probe_cam = np.array([p["camera"] for p in probes])

BANDS = [("40-60", 40, 60), ("60-80", 60, 80), ("80-120", 80, 120), ("120+", 120, 1e9)]


def dprime(g, i):
    return (i.mean() - g.mean()) / np.sqrt(0.5 * (g.var() + i.var()))


def tar_at_far(g, i, far):
    thr = np.quantile(i, far)  # impostor distances below thr are false accepts
    return float((g <= thr).mean()), float(thr)


def evaluate(emb):
    R, P = emb[:n_ref], emb[n_ref:]
    dist = 1.0 - P @ R.T  # cosine distance, (probes, refs)
    per_id = np.stack([dist[:, ref_person == p].min(axis=1) for p in people], axis=1)
    idx = {p: k for k, p in enumerate(people)}
    own = np.array([idx[p] for p in probe_person])
    genuine = per_id[np.arange(len(P)), own]
    masked = per_id.copy()
    masked[np.arange(len(P)), own] = np.inf
    impostor = masked.min(axis=1)
    rank1 = per_id.argmin(axis=1) == own
    return genuine, impostor, rank1


def line(label, g, i, r1):
    if len(g) == 0:
        return f"| {label} | 0 | | | | | |"
    t2, thr2 = tar_at_far(g, i, 1e-2)
    t3, thr3 = tar_at_far(g, i, 1e-3)
    return (
        f"| {label} | {len(g)} | {r1.mean() * 100:.1f} % | {dprime(g, i):.2f} | "
        f"{np.median(g):.3f} / {np.median(i):.3f} | {t2 * 100:.1f} % (≤{thr2:.3f}) | "
        f"{t3 * 100:.1f} % (≤{thr3:.3f}) |"
    )


if __name__ != "__main__":
    models = []
else:
    models = sys.argv[1:] or ["topofr", "auraface", "lvface_b", "facemoe_tinyface", "facemoe_briar"]
if __name__ == "__main__":
    print(f"probes {len(probes)} · refs {n_ref} · identities {people}")
print("probes per person:", {p: int((probe_person == p).sum()) for p in people})
print("refs per person:  ", {p: int((ref_person == p).sum()) for p in people})
print()
hdr = "| model · slice | n | rank-1 | d′ | median genuine / impostor | TAR@FAR 1e-2 (thr) | TAR@FAR 1e-3 (thr) |"
sep = "|---|---|---|---|---|---|---|"
for m in models:
    f = OUT / f"emb_{m}.npy"
    if not f.exists():
        print(f"## {m}: no embeddings\n")
        continue
    g, i, r1 = evaluate(np.load(f))
    print(f"## {m}\n{hdr}\n{sep}")
    print(line("all (≥40 px)", g, i, r1))
    fv = probe_fv
    print(line("face-verified tracks", g[fv], i[fv], r1[fv]))
    print(line("body-anchored tracks", g[~fv], i[~fv], r1[~fv]))
    for name, lo, hi in BANDS:
        sel = (probe_px >= lo) & (probe_px < hi)
        print(line(f"{name} px", g[sel], i[sel], r1[sel]))
    for p in people:
        sel = probe_person == p
        if sel.sum():
            print(line(f"probe {p}", g[sel], i[sel], r1[sel]))
    print()
