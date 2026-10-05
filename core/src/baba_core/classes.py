"""Re-ID class grouping.

The detector (RT-DETRv2 / D-FINE, COCO-trained) reliably labels a vehicle
*as some vehicle* but flips between `car`, `truck`, `bus`, `motorcycle`
frame-to-frame on the same physical object — especially at distance or
under odd lighting. Strict same-class matching would block valid re-ID
hits across those tracks; grouping the COCO vehicle classes together
lets the embedder (which sees the actual pixels) decide identity.

People stay alone — DINOv2 features for "person" are precise enough that
we don't need a fuzzy class gate.

Animal classes also stay alone. The intra-class signal (one dog vs
another) is what we want; mixing in cats would only create false merges.

If we later add vendor- or location-specific classes (e.g., "delivery_van"
from a custom fine-tune), add their COCO id to VEHICLE_GROUP. The map is
keyed by class id so consumers can do `CLASS_GROUPS[track.class_id]` and
get the full set in O(1).

COCO class id refresher (kept short; full list in COCO_CLASSES):
    0 person       1 bicycle    2 car          3 motorcycle
    5 bus          7 truck      14 bird        15 cat        16 dog
"""

from __future__ import annotations

# All four wheeled / motorised vehicle classes get pooled. Bicycle is
# excluded because its silhouette and embedding are distinct enough that
# pooling it with motorcycles hurts more than it helps.
VEHICLE_GROUP: frozenset[int] = frozenset({2, 3, 5, 7})  # car, motorcycle, bus, truck

# Pets: dog and cat get pooled because the detector flips between the two
# on the same physical animal frame-to-frame, especially at distance, in
# low light, or when the pet is curled up. DINOv2 features are distinctive
# enough to tell a black cat from a white dog even when the class label
# disagrees with itself across frames — letting both classes share a
# candidate pool lets the embedding decide identity.
#
# Bird is NOT pooled with cat/dog: its silhouette is wildly different and
# the detector almost never confuses birds with mammals, so pooling just
# opens the door to noise. Other COCO animals (horse, cow, sheep, …) are
# rare on home cameras; keep them singleton until a real use case lands.
PET_GROUP: frozenset[int] = frozenset({15, 16})  # cat, dog

# Map class_id → the set of classes it should re-ID against (always
# includes itself). Classes not listed implicitly form a singleton group
# of themselves — consumers should treat a missing key as `{class_id}`.
CLASS_GROUPS: dict[int, frozenset[int]] = {
    **{cid: VEHICLE_GROUP for cid in VEHICLE_GROUP},
    **{cid: PET_GROUP for cid in PET_GROUP},
}


def group_for(class_id: int) -> frozenset[int]:
    """Return the set of class ids that should be considered the same
    "thing" for re-ID purposes. Always includes the input class. Unknown
    classes fall back to a singleton group containing only themselves."""
    return CLASS_GROUPS.get(class_id, frozenset({class_id}))


# --------------------------------------------------------------------------
# Canonical vocabulary for off-box consumers (the DIDA state contract).
#
# BABA folds COCO on ITS side so the consumer never translates and never
# guesses: every published object_class is one of person|vehicle|animal|other,
# or `none` when nothing is present. `none` matters as much as the rest — a
# consumer mirrors what we push, so a class left at its last value would latch
# an automation on forever.
# --------------------------------------------------------------------------

CANONICAL_NONE = "none"

# All COCO animals (bird…giraffe), a superset of PET_GROUP: for presence
# semantics "an animal is in the zone" is the useful fact, and the re-ID
# reasons for keeping bird out of PET_GROUP don't apply to it.
ANIMAL_GROUP: frozenset[int] = frozenset(range(14, 24))

PERSON_CLASS_ID = 0

# Deliberately NOT VEHICLE_GROUP: that set exists for re-ID pooling and leaves
# bicycle out because pooling its embedding with motorcycles hurts matching —
# an identity concern, not a semantic one. For presence ("a vehicle is on the
# driveway") a bicycle plainly IS a vehicle, so the canonical fold adds it back.
# Reusing the re-ID set here would leak an embedding detail into the contract.
_CANONICAL_VEHICLES: frozenset[int] = VEHICLE_GROUP | frozenset({1})  # + bicycle


def canonical_class(class_id: int) -> str:
    """COCO class id → the canonical vocabulary: person|vehicle|animal|other.

    Never returns `none` — absence is the caller's business (there is no class
    id for "nothing"); use CANONICAL_NONE when no object is present."""
    if class_id == PERSON_CLASS_ID:
        return "person"
    if class_id in _CANONICAL_VEHICLES:
        return "vehicle"
    if class_id in ANIMAL_GROUP:
        return "animal"
    return "other"


# COCO 80-class schema — used by RT-DETRv2 and D-FINE COCO checkpoints.
# Single source of truth: the detector's postprocessor maps class ids to
# names with this, and the api's detection-rules catalogue derives its
# picker from it. Index == COCO class id.
COCO_CLASSES: tuple[str, ...] = (
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train",
    "truck", "boat", "traffic light", "fire hydrant", "stop sign",
    "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow",
    "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella", "handbag",
    "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball", "kite",
    "baseball bat", "baseball glove", "skateboard", "surfboard",
    "tennis racket", "bottle", "wine glass", "cup", "fork", "knife", "spoon",
    "bowl", "banana", "apple", "sandwich", "orange", "broccoli", "carrot",
    "hot dog", "pizza", "donut", "cake", "chair", "couch", "potted plant",
    "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote",
    "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear",
    "hair drier", "toothbrush",
)


def class_id_for_name(name: str | None) -> int | None:
    try:
        return COCO_CLASSES.index(name) if name else None
    except ValueError:
        return None
