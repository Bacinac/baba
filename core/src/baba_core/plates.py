"""Turning noisy plate readings into an identification.

Reading a plate off surveillance footage does not produce a string, it produces
a distribution. Measured on this system's own footage (west, 25.07, 30 readings
of one parked car): the detector found the plate on every frame at 0.88-0.91,
and the OCR returned 24 different strings for it. Four of the eight characters
came back stable and correct; the rest split three ways.

The instinct is to raise a confidence threshold and only accept clean reads.
That fails here for a reason worth stating, because it decides the whole design:
one position had **100% agreement across all thirty readings and was wrong** —
'5' where the plate says '6'. Every frame sees the same plate at the same angle,
so every frame makes the same mistake. Agreement measures how consistent the
viewpoint is, not how right the reading is, and no threshold over it can be a
safety mechanism.

What does work is refusing to read the plate in the open and instead asking a
narrower question: which of the plates we already know is this? That turns eight
unreliable characters into a comparison between a handful of candidates, and it
is the same shape as face matching — a gallery, a distance, and a margin that
must be cleared before anything is claimed.

What sharpens the comparison is that the errors are not uniform. Z is misread
as L, G as B or E, 6 as 5 — glyphs that look alike under blur. A substitution
between two such glyphs is weak evidence that two plates differ, and costs less
here than one between glyphs that look nothing like each other.

A registration-area prior was tried and removed. The idea was sound — Croatian
plates open with one of ~35 area codes, so 'LB' cannot be a real prefix — but
measured on the same thirty readings it snapped 'LB' to 'SB' rather than the
true 'ZG', because the misread prefix happened to sit nearer a different town.
The prefix positions were 97% and 57% agreed and both wrong, so there was no
signal there to correct with, and snapping only turned a wrong guess into a
confident one.

The honest outcome of that footage, run through everything below: nothing is
identified. The winner sits 4.0 away from a plate eight characters long, which
is past what any margin can rescue, and the module refuses. That is the correct
answer for a plate 46 px wide — the decision layer cannot manufacture evidence
the pixels never carried.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta as _delta
from statistics import median
from typing import Any

__all__ = [
    "PlateMatch",
    "PlateSighting",
    "PlateVote",
    "drop_fixtures",
    "drop_standing",
    "even_times",
    "focus_times",
    "match_gallery",
    "moving_samples",
    "normalise_plate",
    "plate_distance",
]

# Glyph pairs the OCR actually confuses on this footage, plus the classic
# lookalikes. Symmetric; a substitution inside a group is cheap because it
# carries almost no information about whether two plates differ.
_CONFUSABLE: tuple[frozenset[str], ...] = (
    frozenset("0OQD"),
    frozenset("1IJLT"),
    frozenset("2Z"),
    frozenset("5S"),
    frozenset("8B"),
    frozenset("6G"),
    frozenset("BE"),
    frozenset("VY"),
    frozenset("KX"),
    frozenset("MN"),
    frozenset("CG"),
    frozenset("UV"),
)
_CONFUSION_COST = 0.4
_SUBSTITUTION_COST = 1.0


def normalise_plate(text: str) -> str:
    """Strip everything that is not an alphanumeric and upper-case the rest.

    Plates are written 'ZG 9420-GZ' by people and 'ZG9420GZ' by an OCR, and the
    separators carry no identity.
    """
    folded = (
        text.upper()
        .replace("Č", "C").replace("Ć", "C").replace("Ž", "Z")
        .replace("Š", "S").replace("Đ", "DJ")
    )
    return re.sub(r"[^A-Z0-9]", "", folded)


def _char_cost(a: str, b: str) -> float:
    if a == b:
        return 0.0
    for group in _CONFUSABLE:
        if a in group and b in group:
            return _CONFUSION_COST
    return _SUBSTITUTION_COST


# A reading shorter than this cannot be trusted to pick between plates, however
# cleanly it aligns: 'GZ' is the tail of half the county. Six characters of a
# seven-or-eight character plate is enough to discriminate; less is not.
_MIN_PARTIAL = 6


def plate_distance(a: str, b: str, *, partial: bool = False) -> float:
    """Confusion-weighted edit distance between two normalised plates.

    Levenshtein rather than positional, because the OCR drops and doubles
    characters as readily as it mistakes them, and a single dropped character
    would otherwise misalign everything after it and drown a real match.

    With `partial`, `a` may align to any stretch of `b` at no charge for the
    parts of `b` it does not cover. The plate detector's box regularly clips
    the leading area code — the one clean reading this system has produced came
    back as '9420GZ' at OCR confidence 1.0, which is ZG9420GZ minus its first
    two characters. Charged as two deletions that reading scores 2.0 and is
    thrown away; aligned as a partial it scores 0.0, which is what it is.
    """
    a, b = normalise_plate(a), normalise_plate(b)
    if not a or not b:
        return float(max(len(a), len(b)))
    if partial and len(a) < _MIN_PARTIAL:
        return float(max(len(a), len(b)))
    # Row 0 is the cost of consuming b's prefix: the usual j for a full
    # comparison, free when a is allowed to start anywhere inside b.
    prev = [0.0 if partial else float(j) for j in range(len(b) + 1)]
    for i, ca in enumerate(a, 1):
        cur = [float(i)]
        for j, cb in enumerate(b, 1):
            cur.append(
                min(
                    prev[j] + 1.0,
                    cur[j - 1] + 1.0,
                    prev[j - 1] + _char_cost(ca, cb),
                )
            )
        prev = cur
    # Likewise the tail of b is free, so the reading may end anywhere in it.
    return min(prev) if partial else prev[-1]


@dataclass
class PlateVote:
    """Per-character votes over the readings of one track.

    The consensus string is what the majority saw at each position. It is
    reported alongside the agreement per position so a caller can see how thin
    the majority was — but agreement is a description, never a gate, for the
    reason in this module's docstring.
    """

    readings: list[str] = field(default_factory=list)

    def add(self, text: str) -> None:
        plate = normalise_plate(text)
        if plate:
            self.readings.append(plate)

    def __len__(self) -> int:
        return len(self.readings)

    def seen(self, text: str) -> int:
        """How many readings returned exactly this string."""
        return sum(1 for r in self.readings if r == text)

    def consensus(self) -> tuple[str, list[float]]:
        if not self.readings:
            return "", []
        length = Counter(len(r) for r in self.readings).most_common(1)[0][0]
        same_length = [r for r in self.readings if len(r) == length] or self.readings
        text, agreement = "", []
        for i in range(length):
            col = Counter(r[i] for r in same_length if len(r) > i)
            if not col:
                break
            char, n = col.most_common(1)[0]
            text += char
            agreement.append(n / sum(col.values()))
        return text, agreement


# A plate that holds still while its supposed car drives past is not on that
# car. Both are read against the car's OWN travel, so a distant approach and
# one under the lens are judged the same way.
_TRAVEL_MIN_WIDTHS = 2.0
_TRAVEL_MIN_FRACTION = 0.25


@dataclass(frozen=True)
class PlateSighting:
    """One plate read in one frame, and where it and its car were at the time.

    Coordinates are the frame's own — any consistent space will do, since every
    comparison here is between two distances measured in it.
    """

    text: str
    confidence: float
    width: float
    at: tuple[float, float]
    # Where the car being read was. None when nothing knows: a track with no
    # samples cannot testify about its own position, and the rule below then
    # has nothing to weigh and leaves the reading alone.
    owner: tuple[float, float] | None = None
    crop: Any = None
    # When this frame was, so a read can record the moment the car showed the
    # plate — the fact the place registry joins on — rather than the moment
    # the sweep got around to deciding, minutes later.
    when: Any = None


def _spread(points: list[tuple[float, float]]) -> float:
    """How far apart the two furthest of these points are."""
    return max(
        (
            math.hypot(a[0] - b[0], a[1] - b[1])
            for i, a in enumerate(points)
            for b in points[i + 1 :]
        ),
        default=0.0,
    )


def drop_fixtures(
    sightings: list[PlateSighting],
) -> tuple[list[PlateSighting], list[tuple[str, float, float, int]]]:
    """Split readings into the ones on this car and the ones on the scenery.

    Grouped by what was read, because that is what a fixture is: the same
    string at the same pixels, frame after frame, however many frames there
    are. Unanimity is its signature rather than its defence — a parked car
    shows the same plate at the same angle every time it is looked at.

    The car's own travel decides where there is any. A car that stopped — at a
    gate, at the end of an approach, reversing into a bay — moves its box no
    further than the scenery does, and there the comparison says nothing; the
    plate's own stillness answers instead, exactly as it does for a read with
    no car box at all. Abstaining there is not neutral, because abstaining
    means keeping.

    Returns the readings to keep, and for each discarded group its text, how
    far the car travelled, how far the plate moved, and how many readings were
    dropped — so the caller can say all of that in its own log.
    """
    groups: dict[str, list[PlateSighting]] = {}
    for s in sightings:
        groups.setdefault(normalise_plate(s.text), []).append(s)
    kept: list[PlateSighting] = []
    dropped: list[tuple[str, float, float, int]] = []
    for text, group in groups.items():
        located = [s for s in group if s.owner is not None]
        if len(located) < 2:
            kept.extend(group)
            continue
        travelled = _spread([s.owner for s in located])  # type: ignore[misc]
        widths = median(s.width for s in located)
        moved = _spread([s.at for s in located])
        if travelled < _TRAVEL_MIN_WIDTHS * widths:
            # The car went nowhere, so there is nothing to compare against and
            # the plate's own stillness is all that is left — the same test
            # `drop_standing` makes when there is no car box at all. Keeping
            # the reading here is what put a parked neighbour's registration
            # on an arriving car: measured 04.09 22:32, seventeen readings of
            # the same string at the same pixel while the car being read was
            # reversing into its bay two spaces over.
            if moved <= _STANDING_MAX_WIDTHS * widths:
                dropped.append((text, travelled, moved, len(group)))
            else:
                kept.extend(group)
            continue
        if moved >= _TRAVEL_MIN_FRACTION * travelled:
            kept.extend(group)
            continue
        dropped.append((text, travelled, moved, len(group)))
    return kept, dropped


# A plate read without a car to measure against has only its own movement to
# testify with. An arriving car crosses the approach, so its plate sweeps a
# multiple of its own width; a car standing in the zone shows the same plate at
# the same pixels for as long as anyone looks.
#
# The bar belongs just above the detector's jitter, NOT near the travel of a
# real approach — a standing plate does not drift, so anything that moves is
# already testifying. Measured: a fixture's plate wanders 2 px while the car it
# was blamed on crossed 228 (shed, 11.09 06:03); the arrival this was first
# calibrated on swept 4.6 widths (west, 26.08). Set at 4.6 widths / 3 it read
# as "arrivals are generous" and cost the opposite mistake: on 11.09 07:59 the
# resident drove into west P1 — a bay under the lens, reached by turning and
# stopping in seconds — his plate swept 106 px against a 107 px bar, all four
# clean readings were binned, and the place stood `unknown` all morning with
# the car in plain sight. 0.3 leaves seven times the jitter below it and five
# times the margin under the tightest arrival seen.
_STANDING_MAX_WIDTHS = 0.3


def drop_standing(
    sightings: list[PlateSighting],
) -> tuple[list[PlateSighting], list[tuple[str, float, int]]]:
    """Keep the plates that CROSSED the window; drop the ones that sat in it.

    The companion to `drop_fixtures` for a read triggered by a place filling
    rather than by a track: there is no car box to measure a plate against, so
    the plate's own travel is the whole test.

    That makes it stricter, and it has to be. `drop_fixtures` can afford to
    abstain when the car stood still, because something else established that
    the car was there at all. Here nothing did: an ALPR zone is a fixed
    rectangle on a driveway, and whatever parks inside it reads perfectly, on
    every frame, forever. A plate that does not move across a window opened by
    somebody else's arrival is not that arrival.

    A single reading of a string cannot testify either way and is dropped with
    the rest of its group — one frame is what a fixture looks like when the
    scan was short.
    """
    groups: dict[str, list[PlateSighting]] = {}
    for s in sightings:
        groups.setdefault(normalise_plate(s.text), []).append(s)
    kept: list[PlateSighting] = []
    dropped: list[tuple[str, float, int]] = []
    for text, group in groups.items():
        moved = _spread([s.at for s in group])
        if moved > _STANDING_MAX_WIDTHS * median(s.width for s in group):
            kept.extend(group)
        else:
            dropped.append((text, moved, len(group)))
    return kept, dropped


# How far outside its own place a plate may sit and still belong to the car
# standing there. A region is drawn on the bodywork, so the plate hangs off its
# near edge and containment would reject the very reading it is meant to keep.
# Measured on shed P2, 05.09 08:56, both cars parked and legible in one frame:
# the plate of the car in the region sits 0.71 of its own widths outside it,
# the plate one bay over sits 8.2 away.
_PLACE_REACH_WIDTHS = 2.0


def _gap(point: tuple[float, float], rect: tuple[float, float, float, float]) -> float:
    x, y = point
    dx = max(rect[0] - x, 0.0, x - rect[2])
    dy = max(rect[1] - y, 0.0, y - rect[3])
    return (dx * dx + dy * dy) ** 0.5


def bind_to_place(
    sightings: list[PlateSighting],
    place: str,
    regions: dict[str, tuple[float, float, float, float]],
) -> tuple[list[PlateSighting], list[tuple[str, str, float]]]:
    """Keep the readings that belong to `place`, judged in the frame's pixels.

    A car read where it stands has geometry and no useful time; a car read
    driving past has time and no reliable geometry. This is the first case, so
    nearness decides — and it has to separate one bay from the next, which at
    these distances is an order of magnitude rather than a close call.

    Every place drawn on the same camera competes: nearest wins, and only then
    is the distance weighed. Without that the wider regions swallow their
    neighbours, west P1 being nearly three times the width of P2 beside it.

    Returns the readings kept, and for each rejected one its text, the place it
    was actually nearest, and how many of its own widths away it sat.
    """
    kept: list[PlateSighting] = []
    rejected: list[tuple[str, str, float]] = []
    for s in sightings:
        if not regions:
            continue
        gaps = {p: _gap(s.at, r) for p, r in regions.items()}
        nearest = min(gaps, key=lambda p: gaps[p])
        width = max(s.width, 1.0)
        if nearest == place and gaps[place] <= _PLACE_REACH_WIDTHS * width:
            kept.append(s)
        else:
            rejected.append((s.text, nearest, gaps[nearest] / width))
    return kept, rejected


@dataclass(frozen=True)
class PlateMatch:
    key: str
    plate: str
    distance: float
    margin: float
    reading: str


def match_gallery(
    reading: str,
    gallery: dict[str, str],
    *,
    max_distance: float = 1.5,
    min_margin: float = 1.5,
) -> PlateMatch | None:
    """Pick the enrolled plate this reading belongs to, or nothing.

    `gallery` maps an opaque key (the identity's global_id) to its enrolled
    plate. Two conditions must both hold: the winner is close enough to be
    credible at all, and it beats the runner-up by `min_margin` — because the
    failure that matters is not "no plate found", it is one household car
    answering to the other's name, and two plates from the same household
    differ by only a few characters.

    `max_distance` is deliberately near the floor. A good reading of an
    enrolled plate lands at 0.0-1.0; the real 46 px reading landed at 4.0. At
    3.0 a Split plate sharing the four digits and both suffix letters
    (ST9648GZ vs ZG9648GZ) was accepted, which is exactly the wrong kind of
    mistake. Refusing costs nothing here — the vehicle is still detected,
    tracked and recorded, it simply is not named.

    A reading too short to be a whole plate is aligned as a partial, since the
    detector's box often clips the area code off the front; a partial still has
    to clear both the ceiling and the margin, and anything under six characters
    is refused outright.

    Returns the winning gallery key with the distance and the margin it
    cleared, so the caller can both act on it and record on what evidence a
    vehicle was named.
    """
    if not gallery:
        return None
    text = normalise_plate(reading)
    if not text:
        return None
    partial = len(text) < 7
    scored = sorted(
        (
            (plate_distance(text, plate, partial=partial), key, plate)
            for key, plate in gallery.items()
        ),
        key=lambda s: s[0],
    )
    best_d, best_key, best_plate = scored[0]
    runner_d = scored[1][0] if len(scored) > 1 else float("inf")
    margin = runner_d - best_d
    if best_d > max_distance or margin < min_margin:
        return None
    return PlateMatch(
        key=best_key, plate=best_plate, distance=best_d, margin=margin, reading=text
    )

# --- which moments of a track are worth decoding ----------------------------

# How far a box must step, as a fraction of its own width, before the step
# counts as movement.
_MOTION_FRACTION = 0.05
# Frames per second to sample. The legible window is ~2 s, so this has to be
# dense enough to land inside it several times.
_SAMPLE_FPS = 4.0


def moving_samples(samples: list[Any]) -> list[Any]:
    """The samples where the vehicle was actually going somewhere.

    A box is travelling when it keeps going the SAME WAY: the step into this
    sample must clear the threshold and point with the step before it. That is
    what separates travel from a resting box breathing, at any amplitude —
    a wobble reverses every frame, so its steps cancel, while distance from a
    reference does not care which way the box went and lets a swing of 8% of
    its own width read as travel on every frame. Measured on the 22.08 shed
    track, that static tail is where every reading came from, and the plate it
    read belonged to the car standing behind.

    Direction is read from the centre AND the width, because a car approaching
    head-on changes width far more than centre. Local, so the span ends where
    the car stops rather than at whatever point it was farthest from its first
    box — a car that reverses into a space travels after that point, and the
    plate reader wants those seconds.
    """
    if len(samples) < 3:
        return []
    ordered = sorted(samples, key=lambda s: s["captured_at"])
    marks = [_step(ordered[i - 1]["bbox"], ordered[i]["bbox"]) for i in range(1, len(ordered))]
    out: list[Any] = []
    for i in range(1, len(marks)):
        (dx, dy, dw), width = marks[i]
        prev = marks[i - 1][0]
        if max(abs(dx), abs(dy), abs(dw)) / width < _MOTION_FRACTION:
            continue
        if dx * prev[0] + dy * prev[1] + dw * prev[2] <= 0:
            continue
        out.append(ordered[i + 1])
    return out


def _step(
    before: Any, after: Any
) -> tuple[tuple[float, float, float], float]:
    """Centre and width displacement from one box to the next, and the scale."""
    ax1, ay1, ax2, ay2 = (float(v) for v in before)
    bx1, by1, bx2, by2 = (float(v) for v in after)
    width = max(1.0, bx2 - bx1)
    return (
        ((bx1 + bx2) - (ax1 + ax2)) / 2.0,
        ((by1 + by2) - (ay1 + ay2)) / 2.0,
        (bx2 - bx1) - (ax2 - ax1),
    ), width


def even_times(start: Any, end: Any, budget: int) -> list[Any]:
    """Evenly spaced moments across a span, ordered in time."""
    span = (end - start).total_seconds()
    if span <= 0:
        return [start]
    wanted = min(budget, max(1, int(span * _SAMPLE_FPS)))
    step = span / max(1, wanted - 1) if wanted > 1 else span
    return [start + _delta(seconds=step * i) for i in range(wanted)]


def focus_times(samples: list[Any], budget: int) -> list[Any]:
    """Evenly spaced moments across the span the car was MOVING.

    A plate seen once anywhere in the approach is worth more than a dense look
    at an arbitrary third of it, so when the span outruns the budget the step
    stretches rather than the span being cut short.
    """
    moving = moving_samples(samples)
    if not moving:
        return []
    return even_times(moving[0]["captured_at"], moving[-1]["captured_at"], budget)

