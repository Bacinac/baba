"""Single-producer / multi-reader shared-memory frame ring.

Ingestor (writer) decodes a camera and stores each frame in its slot of a
fixed-size ring. Readers (detector, tracker, embedder, event-manager,
state-evaluator, api live view) attach to the same segment by name and look
up a frame by the sequence number a message carries, or take the latest.

Why not NATS for full-res frames: a 1080p RGB frame is ~6 MB, an 8-camera
deployment at 5 fps inlined over NATS is ~240 MB/s — workable but wasteful,
and it adds an unnecessary serialize/copy round trip when both sides live
on the same host (which they do in our compose setup).

Layout
------
A single POSIX shm segment per camera, named `baba_cam_{slug}`. Header at
the start, then N_FRAMES fixed-size slots:

    [ HEADER | slot_meta[0..N) | pixels[0..N) ]

Each slot's `pixels` region is sized for the worst case at create time
(width × height × 3 bytes). Cameras can't change resolution at runtime
(restart of the worker recreates the ring), so the size is fixed for the
ring's lifetime.

`slot_meta` carries `sequence` + `pts_ns` + `bytes_used` per slot. The
writer:

  1. picks slot = write_index % N
  2. writes pixels into slot[slot]
  3. atomically writes (sequence, pts_ns, bytes_used) into the meta slot
  4. publishes the new write_index in the header

The reader:

  1. reads write_index from the header
  2. scans slot_meta backwards looking for the target sequence
  3. reads pixels from that slot

Because writes are monotonic and N is small (16 by default), the worst-case
scan is O(16). The classic "torn read" race — reader reads pixels while
writer is mid-overwrite — is detected by the reader re-reading the slot's
`sequence` after the pixel copy; if it changed, the slot was reused and
the read is discarded.
"""

from __future__ import annotations

import contextlib
import logging
import os
import struct
from dataclasses import dataclass
from multiprocessing import resource_tracker, shared_memory

import numpy as np

log = logging.getLogger(__name__)


def _disable_resource_tracker_for_shared_memory() -> None:
    """Stop multiprocessing's resource_tracker from claiming/unlinking
    SharedMemory segments we only *attach* to.

    Background: `multiprocessing.shared_memory.SharedMemory(create=False)`
    registers the segment with this process's resource_tracker. When the
    process exits, the tracker unlinks any segment it knows about — even
    ones it didn't create. That's CPython bug python/cpython#82300 (open
    since 2019) and means a transient reader (or even a one-off
    `docker exec python -c ...` probe) can wipe out an unrelated writer's
    ring. The workaround everyone uses is to no-op the tracker
    registration for the 'shared_memory' resource type.

    Call this once on the reader side before the first SharedMemory()
    attach. The writer side keeps default behaviour so its own segments
    are properly unlinked at shutdown.
    """
    rt = resource_tracker
    if getattr(rt, "_baba_patched", False):
        return
    orig_register = rt.register
    orig_unregister = rt.unregister
    orig_maybe = rt.maybe_unlink if hasattr(rt, "maybe_unlink") else None

    def _filtered_register(name, rtype):
        if rtype == "shared_memory":
            return
        return orig_register(name, rtype)

    def _filtered_unregister(name, rtype):
        if rtype == "shared_memory":
            return
        return orig_unregister(name, rtype)

    rt.register = _filtered_register
    rt.unregister = _filtered_unregister
    if orig_maybe is not None:

        def _filtered_maybe(name, rtype):
            if rtype == "shared_memory":
                return
            return orig_maybe(name, rtype)

        rt.maybe_unlink = _filtered_maybe
    rt._baba_patched = True


_MAGIC = b"BABARING"
# v1: RGB only, no pixel_format field.
# v2: adds 8-byte pixel_format ASCII field at end of header. "rgb" or "nv12".
#     Readers/writers built against v1 cannot attach to v2 (header size and
#     fields differ); BABA rebuilds the base image and all FROM-base
#     services together so the cutover is atomic in practice.
_HEADER_VERSION = 2

# Header struct: magic(8) + version(I) + n_frames(I) + width(I) + height(I)
#              + channels(I) + dtype_code(I) + slot_meta_offset(Q) + pixels_offset(Q)
#              + write_index(Q) + frame_bytes(Q) + pixel_format(8s)
_HEADER_FMT = "<8sIIIIIIQQQQ8s"
_HEADER_SIZE = struct.calcsize(_HEADER_FMT)
# Byte offset of the write_index Q field inside the header. Computed via the
# struct layout: magic(8) + 6×I(24) + slot_meta_offset(Q,8) + pixels_offset(Q,8)
# = 48. Used by push() to publish the bumped write_index without re-packing
# the whole header. Getting this wrong silently clobbers a neighbouring field.
_WRITE_INDEX_OFFSET = 8 + 6 * 4 + 8 + 8
# Per-slot meta: sequence(q) + pts_ns(q) + bytes_used(Q) + reserved(Q)
_SLOT_META_FMT = "<qqQQ"
_SLOT_META_SIZE = struct.calcsize(_SLOT_META_FMT)
# Round meta block up to a cache line per slot to avoid false sharing
# between the writer's slot N and the reader scanning slot N-1.
_SLOT_META_PADDED = 64
# Both pixel formats are uint8; widen the enum if a float format (e.g. depth)
# ever arrives.
_DTYPE_UINT8 = 1

# Valid pixel formats. Stored as 8-byte NUL-padded ASCII in the header.
PIXEL_FORMAT_RGB = "rgb"
PIXEL_FORMAT_NV12 = "nv12"
_VALID_PIXEL_FORMATS = {PIXEL_FORMAT_RGB, PIXEL_FORMAT_NV12}


def _encode_pixel_format(fmt: str) -> bytes:
    """8-byte fixed-width encoding: NUL-padded ASCII, truncated if too long."""
    return fmt.encode("ascii")[:8].ljust(8, b"\x00")


def _decode_pixel_format(raw: bytes) -> str:
    return raw.rstrip(b"\x00").decode("ascii")


def _frame_bytes_for(width: int, height: int, pixel_format: str) -> int:
    """Bytes per frame in the chosen pixel format.

    rgb  → H * W * 3 bytes, ndarray (H, W, 3) uint8
    nv12 → H * W * 3 / 2 bytes, ndarray (H + H/2, W) uint8 flat
           (Y plane occupies the first H rows; UV interleaved plane the
           next H/2 rows). H must be even for valid NV12 layout.
    """
    if pixel_format == PIXEL_FORMAT_RGB:
        return width * height * 3
    if pixel_format == PIXEL_FORMAT_NV12:
        if height % 2 or width % 2:
            raise ValueError(f"NV12 requires even dimensions; got {width}x{height}")
        return width * height * 3 // 2
    raise ValueError(f"unsupported pixel_format: {pixel_format!r}")


def _shm_name(camera_slug: str) -> str:
    """POSIX-safe shm segment name. Linux strips a leading '/' internally
    but `multiprocessing.shared_memory` is happier without it."""
    return f"baba_cam_{camera_slug}"


@dataclass(slots=True, frozen=True)
class FrameRingHeader:
    n_frames: int
    width: int  # logical picture width
    height: int  # logical picture height (for NV12 this is the Y plane height)
    channels: int  # 3 for RGB, 1 for NV12 (semantically not channels, but kept
    # for backward layout compat with v1 readers' parser)
    frame_bytes: int
    slot_meta_offset: int
    pixels_offset: int
    pixel_format: str  # "rgb" | "nv12"; see PIXEL_FORMAT_* constants above


@dataclass(slots=True, frozen=True)
class RingFrame:
    """A frame view backed by the ring buffer. `pixels` is a *copy* — the
    reader must not hold a reference into the shm segment beyond the call
    that returned it, since the writer can overwrite at any time.

    Shape of `pixels` depends on `pixel_format`:
      "rgb"  → (H, W, 3) uint8
      "nv12" → (H + H/2, W) uint8 flat; Y plane on top, interleaved UV below.
               Logical dimensions are (height, width) from the header — use
               those, not pixels.shape, when the picture's W,H matter.
    """

    sequence: int
    pts_ns: int
    pixels: np.ndarray
    pixel_format: str = PIXEL_FORMAT_RGB
    width: int = 0  # logical picture width (header.width)
    height: int = 0  # logical picture height (header.height)


def _pack_header(h: FrameRingHeader, write_index: int) -> bytes:
    return struct.pack(
        _HEADER_FMT,
        _MAGIC,
        _HEADER_VERSION,
        h.n_frames,
        h.width,
        h.height,
        h.channels,
        _DTYPE_UINT8,
        h.slot_meta_offset,
        h.pixels_offset,
        write_index,
        h.frame_bytes,
        _encode_pixel_format(h.pixel_format),
    )


def _unpack_header(buf: memoryview) -> tuple[FrameRingHeader, int]:
    unpacked = struct.unpack(_HEADER_FMT, bytes(buf[:_HEADER_SIZE]))
    magic, version, n, w, h, c, dtc, smo, po, wi, fb, pf_raw = unpacked
    if magic != _MAGIC:
        raise ValueError(f"bad ring magic: {magic!r}")
    if version != _HEADER_VERSION:
        raise ValueError(
            f"unsupported ring version: {version} (expected {_HEADER_VERSION}). "
            "If a writer was built before the v2 header bump, rebuild the base "
            "image and all FROM-base services to pick up the new layout."
        )
    if dtc != _DTYPE_UINT8:
        raise ValueError(f"unsupported ring dtype: {dtc}")
    pixel_format = _decode_pixel_format(pf_raw)
    if pixel_format not in _VALID_PIXEL_FORMATS:
        raise ValueError(f"unknown pixel_format in ring header: {pixel_format!r}")
    return (
        FrameRingHeader(
            n_frames=n,
            width=w,
            height=h,
            channels=c,
            frame_bytes=fb,
            slot_meta_offset=smo,
            pixels_offset=po,
            pixel_format=pixel_format,
        ),
        wi,
    )


def _segment_size(n_frames: int, frame_bytes: int) -> int:
    return _HEADER_SIZE + n_frames * _SLOT_META_PADDED + n_frames * frame_bytes


class FrameRingWriter:
    """Producer side. Create once per camera worker, push frames as they
    arrive from the decoder. Closing unlinks the shm segment so the next
    worker start gets a fresh one."""

    def __init__(
        self,
        camera_slug: str,
        width: int,
        height: int,
        n_frames: int = 16,
        channels: int = 3,
        pixel_format: str = PIXEL_FORMAT_RGB,
    ) -> None:
        if pixel_format not in _VALID_PIXEL_FORMATS:
            raise ValueError(
                f"FrameRingWriter pixel_format must be one of {sorted(_VALID_PIXEL_FORMATS)}; "
                f"got {pixel_format!r}"
            )
        self._slug = camera_slug
        self._name = _shm_name(camera_slug)
        self._channels = channels
        self._pixel_format = pixel_format
        # frame_bytes is computed from the pixel format rather than the
        # nominal channels arg so callers don't have to special-case NV12
        # at every construction site. The `channels` arg is kept for the
        # RGB path's sanity check in push() and to keep the header layout
        # comparable with v1 dumps for forensic tooling.
        frame_bytes = _frame_bytes_for(width, height, pixel_format)
        size = _segment_size(n_frames, frame_bytes)
        # If a previous run left a segment behind (worker crashed without
        # unlink), reuse it only if its dimensions match. Otherwise unlink
        # and recreate, since old slot pointers would be wrong.
        try:
            self._shm = shared_memory.SharedMemory(name=self._name, create=True, size=size)
            created = True
        except FileExistsError:
            existing = shared_memory.SharedMemory(name=self._name)
            if existing.size != size:
                log.info(
                    "ring for %s exists but size mismatch (%d vs %d); recreating",
                    camera_slug,
                    existing.size,
                    size,
                )
                existing.close()
                existing.unlink()
                self._shm = shared_memory.SharedMemory(name=self._name, create=True, size=size)
                created = True
            else:
                self._shm = existing
                created = False
        self._header = FrameRingHeader(
            n_frames=n_frames,
            width=width,
            height=height,
            channels=channels,
            frame_bytes=frame_bytes,
            slot_meta_offset=_HEADER_SIZE,
            pixels_offset=_HEADER_SIZE + n_frames * _SLOT_META_PADDED,
            pixel_format=pixel_format,
        )
        self._write_index = 0
        # Write the header. From here on, readers can attach.
        self._shm.buf[:_HEADER_SIZE] = _pack_header(self._header, self._write_index)
        # Zero slot meta so readers don't see stale sequences.
        for i in range(n_frames):
            self._set_slot_meta(i, sequence=-1, pts_ns=0, bytes_used=0)
        log.info(
            "shm ring %s: created=%s size=%.1f MB n_frames=%d frame=%dx%d fmt=%s",
            self._name,
            created,
            size / 1e6,
            n_frames,
            width,
            height,
            pixel_format,
        )

    @property
    def width(self) -> int:
        return self._header.width

    @property
    def height(self) -> int:
        return self._header.height

    @property
    def pixel_format(self) -> str:
        return self._header.pixel_format

    def _set_slot_meta(self, slot: int, sequence: int, pts_ns: int, bytes_used: int) -> None:
        off = self._header.slot_meta_offset + slot * _SLOT_META_PADDED
        struct.pack_into(
            _SLOT_META_FMT,
            self._shm.buf,
            off,
            sequence,
            pts_ns,
            bytes_used,
            0,
        )

    def push(self, frame: np.ndarray, sequence: int, pts_ns: int) -> None:
        """Write one frame into the next slot. The frame must match the
        ring's configured layout (different per pixel_format); resizing
        the ring at runtime isn't supported.

        Expected shape per pixel_format:
          rgb  → (height, width, 3) uint8
          nv12 → (height + height/2, width) uint8 flat
        """
        if self._pixel_format == PIXEL_FORMAT_RGB:
            h = frame.shape[0] if frame.ndim >= 1 else 0
            w = frame.shape[1] if frame.ndim >= 2 else 0
            c = frame.shape[2] if frame.ndim == 3 else 1
            expected_h, expected_w, expected_c = (
                self._header.height,
                self._header.width,
                self._header.channels,
            )
            if (h, w, c) != (expected_h, expected_w, expected_c):
                log.warning(
                    "ring %s rgb shape mismatch: got %dx%dx%d, expected %dx%dx%d; dropping",
                    self._slug,
                    h,
                    w,
                    c,
                    expected_h,
                    expected_w,
                    expected_c,
                )
                return
        else:  # nv12 — flat (H + H/2, W) uint8
            if frame.ndim != 2:
                log.warning(
                    "ring %s nv12 ndim mismatch: got %d, expected 2; dropping",
                    self._slug,
                    frame.ndim,
                )
                return
            expected_flat_h = self._header.height + self._header.height // 2
            if frame.shape != (expected_flat_h, self._header.width):
                log.warning(
                    "ring %s nv12 shape mismatch: got %s, expected (%d, %d); dropping",
                    self._slug,
                    tuple(frame.shape),
                    expected_flat_h,
                    self._header.width,
                )
                return
        slot = self._write_index % self._header.n_frames
        pixel_off = self._header.pixels_offset + slot * self._header.frame_bytes
        nbytes = frame.nbytes
        # Invalidate slot first (sequence = -1) so a concurrent reader that
        # was about to consume this slot rejects the torn read.
        self._set_slot_meta(slot, sequence=-1, pts_ns=0, bytes_used=0)
        self._shm.buf[pixel_off : pixel_off + nbytes] = (
            frame.tobytes() if not frame.flags["C_CONTIGUOUS"] else memoryview(frame).cast("B")
        )
        # Publish metadata only after pixels are stable.
        self._set_slot_meta(slot, sequence=sequence, pts_ns=pts_ns, bytes_used=nbytes)
        self._write_index += 1
        # Update write_index in header so attaching readers know where to look.
        # Offset comes from the struct layout (see _WRITE_INDEX_OFFSET). The
        # previous implementation used `_HEADER_SIZE - 16` which actually
        # targets the frame_bytes field — every push silently overwrote
        # frame_bytes with the sequence counter, leaving readers to multiply
        # slots by a bogus stride and producing the classic purple/green
        # chroma corruption in NV12 crops.
        struct.pack_into("<Q", self._shm.buf, _WRITE_INDEX_OFFSET, self._write_index)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._shm.close()
        try:
            self._shm.unlink()
        except FileNotFoundError:
            pass
        except Exception:
            log.exception("unlink ring %s failed", self._name)


class FrameRingReader:
    """Consumer side. Attach by camera slug; misses (sequence not in ring,
    or ring not present) return None. Reader is responsible for caching.

    Self-healing on writer restart: when the producer (ingestor worker)
    rebuilds the shm segment — the camera changed resolution, or the worker
    restarted — the old POSIX name is unlinked and re-created with a fresh
    inode. A reader that attached before keeps a mapping of the unlinked
    segment, which still holds its last frames. Every read therefore first
    compares the inode behind the name with the mapped one (one stat on
    tmpfs) and re-attaches on a change. Checking only on a miss was not
    enough: `get_latest` never misses on the old segment, so a long-lived
    reader served its last frame forever, and `get_by_sequence` served a
    stale frame whenever a restarted writer reused a sequence number.
    """

    def __init__(self, camera_slug: str) -> None:
        # Must happen *before* any SharedMemory() attach, on any thread.
        _disable_resource_tracker_for_shared_memory()
        self._slug = camera_slug
        self._name = _shm_name(camera_slug)
        self._shm: shared_memory.SharedMemory | None = None
        self._header: FrameRingHeader | None = None
        self._attached_ino: int | None = None

    def _attach(self) -> bool:
        if self._shm is not None:
            return True
        try:
            self._shm = shared_memory.SharedMemory(name=self._name)
        except FileNotFoundError:
            return False
        try:
            self._header, _ = _unpack_header(memoryview(self._shm.buf))
        except Exception:
            log.exception("ring %s: header parse failed; detaching", self._name)
            self._shm.close()
            self._shm = None
            return False
        # From the descriptor, not the name: the name may already point at a
        # newer segment than the one just mapped.
        self._attached_ino = os.fstat(self._shm._fd).st_ino
        return True

    def _current(self) -> bool:
        """Attached to the segment that bears this camera's name right now."""
        try:
            ino = os.stat(f"/dev/shm/{self._name}").st_ino
        except FileNotFoundError:
            if self._shm is not None:
                log.info("ring %s: segment gone, detaching", self._name)
                self.detach()
            return False
        if self._shm is not None and ino != self._attached_ino:
            log.info(
                "ring %s: segment recreated (inode %d → %d), re-attaching",
                self._name,
                self._attached_ino,
                ino,
            )
            self.detach()
        return self._attach()

    def detach(self) -> None:
        if self._shm is not None:
            with contextlib.suppress(Exception):
                self._shm.close()
            self._shm = None
            self._header = None
            self._attached_ino = None

    def get_by_sequence(self, sequence: int) -> RingFrame | None:
        """Return the frame matching `sequence`, or None if not in ring.
        Detects torn reads by re-checking the slot's sequence after the
        pixel copy and retries once on mismatch."""
        if not self._current() or self._shm is None or self._header is None:
            return None
        h = self._header
        # Scan all slots — N is small (16). Walking from current write_index
        # backwards would marginally improve average case but isn't worth
        # the extra complexity at N=16.
        for slot in range(h.n_frames):
            for _attempt in range(2):
                off = h.slot_meta_offset + slot * _SLOT_META_PADDED
                seq, pts_ns, nbytes, _ = struct.unpack(
                    _SLOT_META_FMT,
                    bytes(self._shm.buf[off : off + _SLOT_META_SIZE]),
                )
                if seq != sequence:
                    break  # try next slot
                if nbytes == 0:
                    return None
                pixel_off = h.pixels_offset + slot * h.frame_bytes
                raw = np.frombuffer(
                    self._shm.buf,
                    dtype=np.uint8,
                    count=nbytes,
                    offset=pixel_off,
                )
                if h.pixel_format == PIXEL_FORMAT_RGB:
                    pixels = raw.reshape(h.height, h.width, h.channels).copy()
                else:  # nv12 — flat (H + H/2, W) uint8
                    pixels = raw.reshape(
                        h.height + h.height // 2,
                        h.width,
                    ).copy()
                # Re-verify the slot wasn't reused mid-copy.
                seq2, pts2, _, _ = struct.unpack(
                    _SLOT_META_FMT,
                    bytes(self._shm.buf[off : off + _SLOT_META_SIZE]),
                )
                if seq2 == sequence and pts2 == pts_ns:
                    return RingFrame(
                        sequence=sequence,
                        pts_ns=pts_ns,
                        pixels=pixels,
                        pixel_format=h.pixel_format,
                        width=h.width,
                        height=h.height,
                    )
                # Torn read; retry once. If the second attempt also fails,
                # the slot has rolled over and the target frame is gone.
        return None

    def get_latest(self) -> RingFrame | None:
        """Return the most recently written frame, or None if the ring is
        absent / empty. Unlike `get_by_sequence`, this serves consumers that
        sample on their OWN cadence (periodic scene-state evaluation, a future
        VLM cold path) and just want "whatever is current" rather than a
        specific sequence tied to a track message.

        Reads the live write_index from the header each call (the `_attach`
        snapshot is stale by design), walks back to the slot the writer most
        recently completed — (write_index - 1) % n_frames — and applies the
        same torn-read re-check as `get_by_sequence`, since the writer may
        roll that slot mid-copy."""
        if not self._current() or self._shm is None or self._header is None:
            return None
        h = self._header
        buf = self._shm.buf
        (live_write_index,) = struct.unpack_from("<Q", buf, _WRITE_INDEX_OFFSET)
        if live_write_index <= 0:
            return None
        slot = (live_write_index - 1) % h.n_frames
        for _attempt in range(2):
            off = h.slot_meta_offset + slot * _SLOT_META_PADDED
            seq, pts_ns, nbytes, _ = struct.unpack(
                _SLOT_META_FMT,
                bytes(buf[off : off + _SLOT_META_SIZE]),
            )
            if nbytes == 0:
                return None
            pixel_off = h.pixels_offset + slot * h.frame_bytes
            raw = np.frombuffer(buf, dtype=np.uint8, count=nbytes, offset=pixel_off)
            if h.pixel_format == PIXEL_FORMAT_RGB:
                pixels = raw.reshape(h.height, h.width, h.channels).copy()
            else:  # nv12 — flat (H + H/2, W) uint8
                pixels = raw.reshape(h.height + h.height // 2, h.width).copy()
            seq2, pts2, _, _ = struct.unpack(
                _SLOT_META_FMT,
                bytes(buf[off : off + _SLOT_META_SIZE]),
            )
            if seq2 == seq and pts2 == pts_ns:
                return RingFrame(
                    sequence=seq,
                    pts_ns=pts_ns,
                    pixels=pixels,
                    pixel_format=h.pixel_format,
                    width=h.width,
                    height=h.height,
                )
            # Torn read — the writer rolled this slot mid-copy; retry once.
        return None

