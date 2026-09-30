import sys
import time

import numpy as np
import openvino as ov

core = ov.Core()
dev = "GPU"
x = np.random.randint(0, 255, (1, 3, 112, 112)).astype(np.float32) / 127.5 - 1
for name, path in [(n, p) for n, p in [("topofr", "/models/face_topofr_r200_glint360k.onnx"),
                   ("lvface_b", "/models/bench_lvface_b.onnx"),
                   ("lvface_l", "/models/bench_lvface_l.onnx")] if n in (sys.argv[1].split(",") if len(sys.argv) > 1 else [n])]:
    for prec in (sys.argv[2:] or ["f32", "f16"]):
        try:
            m = core.read_model(path)
            m.reshape({m.inputs[0].get_any_name(): [1, 3, 112, 112]})
            t0 = time.time()
            c = core.compile_model(m, dev, {"INFERENCE_PRECISION_HINT": prec, "PERFORMANCE_HINT": "LATENCY"})
            compile_s = time.time() - t0
            req = c.create_infer_request()
            for _ in range(10):
                req.infer({0: x})
            t = time.time()
            n = 60
            for _ in range(n):
                req.infer({0: x})
            ms = (time.time() - t) / n * 1000
            print(f"{name:9s} {prec}: {ms:6.1f} ms/face  (compile {compile_s:.0f}s)", flush=True)
        except RuntimeError as e:
            print(f"{name:9s} {prec}: FAILED {str(e)[:160]}", flush=True)
