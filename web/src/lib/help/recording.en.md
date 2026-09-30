Cameras record **continuously, 24/7**, whether or not BABA sees anything — one stream per camera, cut into 5-minute segments, never gated on detection. So you never miss a moment because the pipeline was briefly wrong. Activity is understanding *on top of* that footage, not a replacement for it.

## Recording mode: continuous vs. activity

In Settings → Recording you pick one of two modes. **Important:** both still record 24/7 — the mode is not a capture switch but a **retention policy** (what is kept afterwards, and what is deleted).

- **Continuous** — everything is kept, bounded only by age (default 7 days) and the disk safety net. Full history.
- **Activity** — past a short window, segments that overlap no visit are deleted, and only a **short margin around activity** is kept (pre/post-roll, default 1 minute). This saves disk, but the quiet parts of the day are not kept permanently.

## Three layers of retention

1. **Age** — segments older than `retention_days` (default 7) are deleted, in both modes.
2. **Activity** — activity mode only; deletes quiet segments as described above.
3. **Disk** — a safety net: when usage crosses the high-water mark (default 85%), the oldest is deleted until it drops to the low mark (75%). It also has a fuse: if deleting frees no space (someone else is filling a shared disk), it stops and alarms rather than wiping all history.

## Clips are cut on demand

There is no separate "event recording". When you click a row in Activity, BABA cuts a clip from the continuous segments for the exact window of that moment and caches it. That is why a vehicle's arrival and departure yield two short clips from the same footage. H.264 cameras are cut without re-encoding (lossless); HEVC is converted to H.264. The clip cache is cleared after 6 hours.

## Two storage tiers

- **Slow tier** (HDD/array) holds the bulk video — the continuous segments. Large, sequential I/O. The disk retention limit is measured here.
- **Fast tier** (SSD/NVMe) holds small random-access files: cached clips, crops, thumbnails, reference photos — plus the database, models and logs.

So bulk video doesn't compete with the latency-sensitive database on one disk.

---

Related: [Activity and visits](/help/activity-and-visits).
