# Third-party licenses

BABA's own code is licensed under [PolyForm Noncommercial 1.0.0](LICENSE.md).
This file covers what BABA downloads and runs beside it: models, runtimes and
services. The BABA name and visuals are not covered by any of these licenses.

This is not legal advice. If you plan commercial use, check with a lawyer.

## Models BABA downloads

`tools/download_models.py` fetches them into `BABA_MODELS_HOST`;
`--models=minimal` fetches only the default detector and re-identification
model. Every one of them is licensed for commercial use.

| Role | Model | License |
|---|---|---|
| Detection (default) | RT-DETRv2 R18 | Apache 2.0 |
| Detection (alternatives) | RT-DETRv2 R34, R50, R101; D-FINE N to X; RF-DETR Nano | Apache 2.0 |
| Re-identification | DINOv2 ViT-S/14, ViT-B/14 | Apache 2.0 |
| Tracking appearance | OSNet x0.25 (MSMT17) | MIT |
| Face detection | YuNet | MIT |
| Face recognition | AuraFace | Apache 2.0 |
| Zones | SAM2 Hiera Tiny | Apache 2.0 |

## Models you bring yourself

BABA never ships or downloads these; you put the weights into
`BABA_MODELS_HOST` and select them in Settings or with an environment variable,
and their license is then yours to satisfy.

- **Ultralytics YOLO** (v5, v8, v10, v11 and later) is AGPL-3.0, and Ultralytics
  holds that the license covers the weights too. Serving a YOLO model over a
  network would put the whole system under the AGPL. YOLOv7 and YOLOv9 are
  GPL-3.0.
- **InsightFace face models** (SCRFD, RetinaFace and the buffalo packs) are for
  non-commercial research only. The Settings page offers them as a swappable
  face stack.
- **License-plate reading** is optional and reads nothing until you bring the
  weights. The default plate detector comes from `open-image-models` and is a
  YOLOv9 derivative, so treat it as GPL-3.0; the text recogniser comes from
  `fast-plate-ocr`.

## Runtimes and services

| Component | License | Note |
|---|---|---|
| TensorRT, CUDA | NVIDIA proprietary | runtime libraries may be redistributed; NVIDIA hardware only |
| OpenVINO | Apache 2.0 | |
| ONNX Runtime | MIT | |
| FFmpeg | LGPL 2.1+ | BABA's images build it without `--enable-gpl` and without nonfree parts (CUDA kernels through clang), so the binaries stay redistributable |
| PyAV | BSD-3-Clause | |
| go2rtc | MIT | |
| Norfair | BSD-3-Clause | |
| NATS | Apache 2.0 | |
| PostgreSQL, pgvector | PostgreSQL License | |
| AI assistant (optional) | provider terms | Anthropic or OpenAI under your own account and API key |

## Patents

Patents are a separate layer from copyright. Transformer detectors are
published openly, and the video-surveillance market has patent holders of its
own (Hikvision, Dahua, Bosch among them); a product built on BABA should have a
patent search done before launch.

Update this file whenever a default model changes, a dependency is added or a
license question comes up.
