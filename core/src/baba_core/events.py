from dataclasses import dataclass
from typing import get_args
from uuid import UUID, uuid5

from baba_core.event_kinds import EventKind

_TRACK_EVENT_KINDS = frozenset(
    kind for kind in get_args(EventKind) if kind.startswith(("zone_", "object_"))
)


@dataclass(frozen=True, slots=True)
class TrackEvent:
    kind: EventKind
    local_track_id: int
    track_id: UUID
    class_id: int
    class_name: str
    bbox: tuple[float, float, float, float]
    zone_id: UUID | None = None

    def __post_init__(self) -> None:
        if self.kind not in _TRACK_EVENT_KINDS:
            raise ValueError("unsupported track event kind")
        if not isinstance(self.track_id, UUID):
            raise ValueError("track event requires a reserved track UUID")
        if self.kind.startswith("zone_") and self.zone_id is None:
            raise ValueError("zone event requires a zone UUID")

    def event_id(self, camera_id: UUID, timestamp_ns: int) -> UUID:
        return uuid5(camera_id, f"{self.track_id}:{self.kind}:{self.zone_id}:{timestamp_ns}")

    def payload(self, *, zone_name: str | None = None, zone_kind: str | None = None) -> dict:
        data = {
            "bbox": list(self.bbox), "class_name": self.class_name,
            "class_id": self.class_id, "track_id": str(self.local_track_id),
        }
        if self.zone_id is not None:
            data.update({"zone_id": str(self.zone_id), "zone_name": zone_name, "zone_kind": zone_kind})
        return data
