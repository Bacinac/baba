"""Paired bootstrap over probes: is the difference between two models real,
and does it survive weighting every person equally (Marko is 82 % of probes)."""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, "/work")
from score import evaluate, people, probe_person, probe_px, tar_at_far

OUT = Path("/work/out")
rng = np.random.default_rng(7)
persons = [p for p in people if (probe_person == p).sum() >= 30]

def metrics(g, i, sel):
    t3, _ = tar_at_far(g[sel], i[sel], 1e-3)
    return t3

def macro(g, i, r1):
    return np.mean([r1[probe_person == p].mean() for p in persons]), \
           np.mean([tar_at_far(g[probe_person == p], i[probe_person == p], 1e-2)[0] for p in persons])

a, b = sys.argv[1], sys.argv[2]
ga, ia, ra = evaluate(np.load(OUT / f"emb_{a}.npy"))
gb, ib, rb = evaluate(np.load(OUT / f"emb_{b}.npy"))
n = len(ga)
print(f"{a} vs {b}: probes {n}, persons for macro {persons}")
for label, sel in [("all", np.ones(n, bool)), ("40-60 px", (probe_px >= 40) & (probe_px < 60))]:
    diffs = []
    for _ in range(400):
        idx = rng.integers(0, n, n)
        s = sel[idx]
        if s.sum() < 50:
            continue
        diffs.append(metrics(ga[idx], ia[idx], s) - metrics(gb[idx], ib[idx], s))
    d = np.array(diffs) * 100
    print(f"  TAR@1e-3 {label}: {a}-{b} = {d.mean():+.1f} pts, 95% CI [{np.percentile(d, 2.5):+.1f}, {np.percentile(d, 97.5):+.1f}]")
ma, mb = macro(ga, ia, ra), macro(gb, ib, rb)
print(f"  macro over persons rank-1: {a} {ma[0]*100:.1f} %  {b} {mb[0]*100:.1f} %")
print(f"  macro over persons TAR@1e-2: {a} {ma[1]*100:.1f} %  {b} {mb[1]*100:.1f} %")
