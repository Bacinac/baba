from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Precision(StrEnum):
    FP32 = "fp32"
    FP16 = "fp16"
    INT8 = "int8"


@dataclass(slots=True, frozen=True)
class DeviceInfo:
    backend: str
    device_id: int
    device_name: str
    supported_precisions: tuple[Precision, ...] = (Precision.FP32,)


@dataclass(slots=True, frozen=True)
class BoundingBox:
    x1: float
    y1: float
    x2: float
    y2: float

    @property
    def width(self) -> float:
        return self.x2 - self.x1

    @property
    def height(self) -> float:
        return self.y2 - self.y1

    @property
    def area(self) -> float:
        return self.width * self.height



@dataclass(slots=True, frozen=True)
class Detection:
    bbox: BoundingBox
    class_id: int
    class_name: str
    confidence: float
