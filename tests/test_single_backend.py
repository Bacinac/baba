"""Every service runs exactly one backend per job, picked from its variant, and
a backend that is missing or fails to initialise stops the service loudly.

make_session (the embedder, face, SAM2 and ReID models) and the onnxruntime
detector backend each bind one execution provider. onnxruntime is replaced by
a stand-in that behaves like the real one where it matters: a GPU provider
that fails to initialise does not raise, the session quietly binds the CPU,
and only get_providers() tells. So every branch runs on any image.
"""

import sys
from types import SimpleNamespace

import pytest
from baba_backend_onnxruntime.backend import ONNXRuntimeBackend
from baba_core import RING_PIXEL_FORMAT, build_variant, onnx_session
from baba_core.backend import BackendConfig
from baba_core.onnx_session import make_session, ort_provider
from baba_core.variant import VARIANTS

CUDA = "CUDAExecutionProvider"
CPU = "CPUExecutionProvider"


class _FakeOrt:
    def __init__(self, available, broken=()):
        self._available = list(available)
        self._broken = set(broken)
        self.requested = []
        self.GraphOptimizationLevel = SimpleNamespace(ORT_ENABLE_ALL=99)

    def get_available_providers(self):
        return self._available

    def SessionOptions(self):
        return SimpleNamespace(add_session_config_entry=lambda *_: None)

    def InferenceSession(self, path, sess_options, providers):
        names = [p[0] if isinstance(p, tuple) else p for p in providers]
        self.requested.append(providers)
        # Like ORT itself: the CPU provider is always appended behind the
        # requested ones, and a provider that is missing or fails to
        # initialise is skipped with a warning, not an exception.
        bound = [n for n in names if n in self._available and n not in self._broken]
        if CPU not in bound and CPU not in self._broken:
            bound.append(CPU)
        if not bound:
            raise RuntimeError(f"{CPU} failed to initialise")
        return SimpleNamespace(
            get_providers=lambda: bound,
            get_inputs=lambda: [],
            get_outputs=lambda: [],
        )


@pytest.fixture
def model(tmp_path):
    p = tmp_path / "m.onnx"
    p.write_bytes(b"\0")
    return p


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("BABA_VARIANT", raising=False)


def _ort(monkeypatch, available, broken=()):
    fake = _FakeOrt(available, broken)
    monkeypatch.setitem(sys.modules, "onnxruntime", fake)
    return fake


def test_the_variant_defaults_to_cpu_and_is_normalised(monkeypatch):
    assert build_variant() == "cpu"
    monkeypatch.setenv("BABA_VARIANT", " NVIDIA ")
    assert build_variant() == "nvidia"


def test_an_unknown_variant_is_a_configuration_error(monkeypatch, model):
    monkeypatch.setenv("BABA_VARIANT", "amd")
    with pytest.raises(RuntimeError, match="is not one of"):
        build_variant()
    fake = _ort(monkeypatch, [CPU])
    with pytest.raises(RuntimeError, match="is not one of"):
        make_session(model)
    assert fake.requested == []


def test_every_variant_names_the_pixel_format_of_its_frame_ring():
    assert set(RING_PIXEL_FORMAT) == set(VARIANTS)
    assert RING_PIXEL_FORMAT["cpu"] == "rgb"
    assert RING_PIXEL_FORMAT["nvidia"] == RING_PIXEL_FORMAT["intel"] == "nv12"


def test_each_onnxruntime_variant_has_exactly_one_provider(monkeypatch):
    assert ort_provider() == CPU
    monkeypatch.setenv("BABA_VARIANT", "nvidia")
    assert ort_provider() == CUDA
    monkeypatch.setenv("BABA_VARIANT", "intel")
    with pytest.raises(RuntimeError, match="runs on OpenVINO"):
        ort_provider()


def test_nvidia_offers_onnxruntime_only_cuda(monkeypatch, model):
    monkeypatch.setenv("BABA_VARIANT", "nvidia")
    fake = _ort(monkeypatch, [CUDA, CPU])
    _, chosen = make_session(model)
    assert chosen == CUDA
    assert fake.requested == [[CUDA]]


def test_nvidia_with_a_failing_cuda_refuses_the_cpu_ort_bound_instead(monkeypatch, model):
    monkeypatch.setenv("BABA_VARIANT", "nvidia")
    _ort(monkeypatch, [CUDA, CPU], broken={CUDA})
    with pytest.raises(RuntimeError, match=f"{CUDA} failed to initialise and onnxruntime bound {CPU}"):
        make_session(model)


def test_nvidia_on_a_wheel_without_cuda_fails_before_loading(monkeypatch, model):
    monkeypatch.setenv("BABA_VARIANT", "nvidia")
    fake = _ort(monkeypatch, [CPU])
    with pytest.raises(RuntimeError, match=f"needs {CUDA}"):
        make_session(model)
    assert fake.requested == []


def test_intel_runs_on_native_openvino_without_touching_onnxruntime(monkeypatch, model):
    monkeypatch.setenv("BABA_VARIANT", "intel")
    ov = object()
    monkeypatch.setattr(onnx_session, "_openvino_session", lambda *a, **kw: ov)
    monkeypatch.setitem(sys.modules, "onnxruntime", None)
    assert make_session(model) == (ov, "OpenVINOExecutionProvider")


def test_intel_without_a_gpu_surfaces_the_openvino_error(monkeypatch, model):
    monkeypatch.setenv("BABA_VARIANT", "intel")

    def no_gpu(*_a, **_kw):
        raise RuntimeError("OpenVINO sees no GPU")

    monkeypatch.setattr(onnx_session, "_openvino_session", no_gpu)
    monkeypatch.setitem(sys.modules, "onnxruntime", None)
    with pytest.raises(RuntimeError, match="no GPU"):
        make_session(model)


def test_the_cpu_build_runs_on_cpu(monkeypatch, model):
    fake = _ort(monkeypatch, [CUDA, CPU])
    _, chosen = make_session(model)
    assert chosen == CPU
    assert fake.requested == [[CPU]]


def test_when_the_cpu_itself_fails_the_error_surfaces(monkeypatch, model):
    _ort(monkeypatch, [CPU], broken={CPU})
    with pytest.raises(RuntimeError, match=f"{CPU} failed to initialise"):
        make_session(model)


def test_a_missing_model_is_named(model):
    with pytest.raises(FileNotFoundError):
        make_session(model.with_name("absent.onnx"))


def test_the_detector_backend_binds_cuda_on_the_configured_device(monkeypatch, model):
    monkeypatch.setenv("BABA_VARIANT", "nvidia")
    fake = _ort(monkeypatch, [CUDA, CPU])
    backend = ONNXRuntimeBackend()
    backend.load(model, BackendConfig(device_id=1))
    assert fake.requested == [[(CUDA, {"device_id": 1})]]
    assert backend.device_info.device_name == CUDA


def test_the_detector_backend_refuses_a_cuda_that_ort_skipped_on_its_own(monkeypatch, model):
    monkeypatch.setenv("BABA_VARIANT", "nvidia")
    _ort(monkeypatch, [CUDA, CPU], broken={CUDA})
    with pytest.raises(RuntimeError, match=f"onnxruntime bound {CPU} instead"):
        ONNXRuntimeBackend().load(model, BackendConfig())


def test_the_detector_backend_on_cpu_runs_on_cpu(monkeypatch, model):
    fake = _ort(monkeypatch, [CPU])
    backend = ONNXRuntimeBackend()
    backend.load(model, BackendConfig())
    assert fake.requested == [[CPU]]
    assert backend._session.get_providers() == [CPU]
