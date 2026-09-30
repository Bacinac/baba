# Face embedder benchmark on BABA's own CCTV faces

Held-out comparison of face embedders on the pixels the pipeline actually
sees: probes are `track_embedding_samples.face_crop_path` (aligned 112x112)
of tracks named to a person identity, references are `identity_reference_photos`
re-detected with SCRFD-10G and aligned by `baba_core.face.align_face`.
Scored the way the matcher decides: MIN cosine distance over an identity's
references; genuine vs nearest other identity; rank-1, d', TAR at FAR.

    data/samples.csv, data/refs.csv   exported with COPY from production
    data/face_crops/, data/reference_photos/
    models/                            *.onnx / *.pt of every candidate
    shim/baba_core/                    face.py face_detectors.py face_models.py onnx_session.py
    facemoe/                           backbones/ from github.com/Kartik-3004/FaceMoE

    docker build -t facebench .
    docker run --rm -v $PWD:/work -w /work facebench python embed.py [models...]
    docker run --rm -v $PWD:/work -w /work facebench python score.py [models...]

2026-09-15 result (1580 probes >=40 px, 167 refs, 6 identities): TopoFR R200
Glint360K ahead of LVFace-B, FaceMoE (tinyface / briar) and AuraFace on every
slice, including 40-60 px. See memory `face_model_bench_2026-09-15`.

Second round the same day ("give LVFace a chance"): flip-test fusion for
both, LVFace-L, paired bootstrap (`compare.py`) and GPU latency on the
production Arc through OpenVINO (`ov_time.py`, run inside baba-embedder).
On the Arc at fp16 TopoFR and LVFace-B tie at ~16 ms/face; the CPU-only 2x
speed gap does not exist on the accelerator BABA runs on.
