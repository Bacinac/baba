from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from baba_core.types import DeviceInfo, Precision

ModelInput = np.ndarray | dict[str, np.ndarray]
ModelOutput = np.ndarray | list[np.ndarray] | dict[str, np.ndarray]


@dataclass(slots=True, frozen=True)
class InputTensorInfo:
    """Describes one of the model's input tensors after the engine is loaded.

    Used by pipeline code to decide whether the model has preprocessing
    baked into its graph (uint8 NHWC input → engine does cast+normalise+
    permute) or expects the caller to do that work upstream (float32 NCHW).
    Shape entries can be -1 for dynamic dimensions; callers must handle
    that or pass concrete inputs that satisfy the engine's optimisation
    profile.
    """

    name: str
    dtype: np.dtype
    shape: tuple[int, ...]


@dataclass(slots=True)
class BackendConfig:
    """User-facing config passed to a backend at load time."""

    device_id: int = 0
    precision: Precision = Precision.FP16
    max_batch_size: int = 8
    cache_dir: Path | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class InferenceBackend(ABC):
    """Abstract base for all hardware backends.

    Implementations live in separate packages registered via Python entry
    point `baba.backends`. The pipeline never imports them directly — it asks
    the registry for a backend by name, then probes the LOADED engine
    (`input_tensors()`) for what it actually needs to know. A declarative
    capabilities surface used to sit here; every backend filled seven fields
    and nothing ever read one, so the contract described an interrogation that
    never happened.
    """

    name: str

    @classmethod
    @abstractmethod
    def is_available(cls) -> bool:
        """Probe the host: drivers present, hw accessible, libs importable."""

    @classmethod
    @abstractmethod
    def enumerate_devices(cls) -> list[DeviceInfo]:
        """Return all devices this backend can drive on the current host."""

    @abstractmethod
    def load(self, model_path: Path, config: BackendConfig) -> None:
        """Load an ONNX model. Backend may convert+cache to native format."""

    @abstractmethod
    def warmup(self, batch_size: int = 1) -> None:
        """Run dummy inference to compile kernels, allocate buffers."""

    @abstractmethod
    def infer(self, inputs: ModelInput) -> ModelOutput:
        """Run synchronous inference. Shape: (B, ...) for batched input."""

    @abstractmethod
    def unload(self) -> None:
        """Free GPU memory, close handles."""

    @property
    @abstractmethod
    def device_info(self) -> DeviceInfo: ...

    def input_tensors(self) -> list[InputTensorInfo]:
        """Describe the model's input tensors. Default implementation
        raises; backends override to read from the loaded engine.

        Pipeline uses this to detect whether preprocessing is baked into
        the model graph (e.g. uint8 NHWC) and skip the equivalent CPU
        work. Backends that can't expose this metadata stay on the
        default; callers must then assume the legacy float32 NCHW
        contract.
        """
        raise NotImplementedError(f"{type(self).__name__} does not expose input_tensors()")

