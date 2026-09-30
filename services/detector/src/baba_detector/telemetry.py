"""Per-camera detection telemetry accumulator.

Collects, per camera and per class, the confidence/size distribution of
PUBLISHED detections and counters of detections the rules DROPPED (by
reason). Flushed once a minute as a DetectorTelemetry message — the
persistent evidence base for threshold tuning ("where is the valley between
the FP cluster and real objects") and for the drop-spike watcher that
catches a camera being silently blinded by an over-tight rule.

Two-threshold aware: the detector publishes down to the MAINTAIN floor, so
`n` and the conf quantiles cover below-birth boxes too and `dropped.conf`
only counts below-MAINTAIN. `n_birth` carries how many published samples
could actually START a track — without it an over-tight BIRTH threshold is
invisible to both the tuner and the watcher (it no longer shows as a drop
spike, because the detections are published rather than dropped).
"""

from __future__ import annotations

from collections import defaultdict

from baba_core.wire import ClassWindowStats, DetectorTelemetry, DroppedWindowStats


def _quantile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, max(0, round(q * (len(sorted_vals) - 1))))
    return float(sorted_vals[idx])


class TelemetryAccumulator:
    # Cap per-class samples per window so a pathological detector burst
    # can't grow memory; at 4 fps × 60 s a camera produces ≤240 samples of
    # one class anyway, so the cap only trims true anomalies (whose p95 the
    # first 1000 samples estimate fine).
    _MAX_SAMPLES = 1000

    def __init__(self) -> None:
        # camera → class → ([confs], [sizes])
        self._published: dict[str, dict[str, tuple[list[float], list[float]]]] = defaultdict(dict)
        # camera → class → reason → count
        self._dropped: dict[str, dict[str, dict[str, int]]] = defaultdict(dict)
        # camera → class → how many PUBLISHED samples were birth-eligible.
        # Incremented only when the sample is actually recorded, so it can
        # never exceed `n` under the _MAX_SAMPLES cap.
        self._birth: dict[str, dict[str, int]] = defaultdict(dict)

    def note_published(
        self,
        camera: str,
        class_name: str,
        conf: float,
        size_pct: float,
        birth_eligible: bool = True,
    ) -> None:
        confs, sizes = self._published[camera].setdefault(class_name, ([], []))
        if len(confs) < self._MAX_SAMPLES:
            confs.append(conf)
            sizes.append(size_pct)
            if birth_eligible:
                per = self._birth[camera]
                per[class_name] = per.get(class_name, 0) + 1

    def note_dropped(self, camera: str, class_name: str, reason: str) -> None:
        per_class = self._dropped[camera].setdefault(class_name, {})
        per_class[reason] = per_class.get(reason, 0) + 1

    def flush(self, window_s: float) -> list[DetectorTelemetry]:
        out: list[DetectorTelemetry] = []
        cameras = set(self._published) | set(self._dropped)
        for cam in cameras:
            published: dict[str, ClassWindowStats] = {}
            for cls, (confs, sizes) in self._published.get(cam, {}).items():
                sc = sorted(confs)
                ss = sorted(sizes)
                published[cls] = ClassWindowStats(
                    n=len(sc),
                    conf_p50=_quantile(sc, 0.5),
                    conf_p95=_quantile(sc, 0.95),
                    size_p50=_quantile(ss, 0.5),
                    size_p95=_quantile(ss, 0.95),
                    n_birth=self._birth.get(cam, {}).get(cls, 0),
                )
            dropped = {
                cls: DroppedWindowStats(
                    conf=reasons.get("conf", 0),
                    size=reasons.get("size", 0),
                    disabled=reasons.get("disabled", 0),
                )
                for cls, reasons in self._dropped.get(cam, {}).items()
            }
            if published or dropped:
                out.append(
                    DetectorTelemetry(
                        camera_id=cam,
                        window_s=window_s,
                        published=published,
                        dropped=dropped,
                    )
                )
        self._published.clear()
        self._dropped.clear()
        self._birth.clear()
        return out
