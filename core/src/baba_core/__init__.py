from baba_core.backend import (
    BackendConfig,
    InferenceBackend,
    InputTensorInfo,
    ModelInput,
    ModelOutput,
)
from baba_core.classes import (
    ANIMAL_GROUP,
    CANONICAL_NONE,
    CLASS_GROUPS,
    COCO_CLASSES,
    PERSON_CLASS_ID,
    PET_GROUP,
    VEHICLE_GROUP,
    canonical_class,
    group_for,
)
from baba_core.dsn import dsn_from_env
from baba_core.embed import (
    EMBED_INPUT_SIZE,
    EMBEDDING_DIM,
    MAX_CROP_EDGE,
    DINOv2OnnxBackend,
    EmbeddingBackend,
    StubBackend,
    cap_long_edge,
)
from baba_core.embed import (
    make_backend as make_embedding_backend,
)
from baba_core.face import (
    FACE_EMBEDDING_DIM,
    ArcFaceStyleEmbedder,
    FaceStack,
    make_face_stack_for_model,
)
from baba_core.face_detectors import (
    DEFAULT_FACE_DETECTOR,
    FACE_DETECTORS,
    FaceDetectorSpec,
    get_face_detector,
    resolve_detector_path,
)
from baba_core.face_models import (
    DEFAULT_FACE_MODEL,
    FACE_MODELS,
    FaceModelSpec,
    get_face_model,
    resolve_model_path,
)
from baba_core.frame_ring import FrameRingReader, FrameRingWriter, RingFrame
from baba_core.go2rtc import go2rtc_auth_from_env
from baba_core.localtime import LOCAL_TZ, local_now, local_today
from baba_core.logconfig import setup_logging
from baba_core.nats_conn import connect as nats_connect
from baba_core.nats_conn import drain_quietly
from baba_core.paths import MediaLayout, StoragePaths
from baba_core.pgvector import parse_vector, vector_literal
from baba_core.recordings import covers_until_sql
from baba_core.registry import BACKENDS, DECODERS
from baba_core.stats import StatsCollector, StatsSnapshot, TimingSummary
from baba_core.types import (
    BoundingBox,
    Detection,
    DeviceInfo,
    Precision,
)
from baba_core.url import mask_credentials
from baba_core.variant import RING_PIXEL_FORMAT, build_variant
from baba_core.video import DecoderCapabilities, DecoderConfig, VideoDecoder

__all__ = [
    "ANIMAL_GROUP",
    "BACKENDS",
    "CANONICAL_NONE",
    "CLASS_GROUPS",
    "COCO_CLASSES",
    "DECODERS",
    "DEFAULT_FACE_DETECTOR",
    "DEFAULT_FACE_MODEL",
    "EMBEDDING_DIM",
    "EMBED_INPUT_SIZE",
    "FACE_DETECTORS",
    "FACE_EMBEDDING_DIM",
    "FACE_MODELS",
    "LOCAL_TZ",
    "MAX_CROP_EDGE",
    "PERSON_CLASS_ID",
    "PET_GROUP",
    "RING_PIXEL_FORMAT",
    "VEHICLE_GROUP",
    "ArcFaceStyleEmbedder",
    "BackendConfig",
    "BoundingBox",
    "DINOv2OnnxBackend",
    "DecoderCapabilities",
    "DecoderConfig",
    "Detection",
    "DeviceInfo",
    "EmbeddingBackend",
    "FaceDetectorSpec",
    "FaceModelSpec",
    "FaceStack",
    "FrameRingReader",
    "FrameRingWriter",
    "InferenceBackend",
    "InputTensorInfo",
    "MediaLayout",
    "ModelInput",
    "ModelOutput",
    "Precision",
    "RingFrame",
    "StatsCollector",
    "StatsSnapshot",
    "StoragePaths",
    "StubBackend",
    "TimingSummary",
    "VideoDecoder",
    "build_variant",
    "canonical_class",
    "cap_long_edge",
    "covers_until_sql",
    "drain_quietly",
    "dsn_from_env",
    "get_face_detector",
    "get_face_model",
    "go2rtc_auth_from_env",
    "group_for",
    "local_now",
    "local_today",
    "make_embedding_backend",
    "make_face_stack_for_model",
    "mask_credentials",
    "nats_connect",
    "parse_vector",
    "resolve_detector_path",
    "resolve_model_path",
    "setup_logging",
    "vector_literal",
]
