A zone is a polygon you draw on the camera image to say "I care about this". Every zone works from the same anchor: the subject's **foot** — the bottom-centre of the box, where the object meets the ground. A polygon drawn once behaves the same everywhere it is evaluated, because it always asks whether the *foot* is inside, not the whole box.

## Zone kinds

- **Interest / entry zone** — you want events here: someone entered, someone lingered. This is the most common zone.
- **Parking zone** — a place where vehicles legitimately sit for days. A parked car there is not suppressed as clutter (see [Tuning](/help/tuning)), and its time is measured as parked duration.
- **Restricted zone** — a place where nothing should be. Unlike an interest zone, it fires even on **stationary** objects: something left there is still an event.
- **Ignore zone** — dead ground. A neighbour's terrace, a public pavement. BABA tracks **nothing** here — detections are dropped at the entrance to the tracker, so they cost neither recognition nor a clip.

An ignore zone is not the answer to static clutter (the self-learning phantom registry handles that) — it is for ground where *real* subjects appear and you simply don't want them.

## Per-class rules

Each zone can carry rules per class: minimum confidence, minimum box size, minimum dwell before an event fires, and a cooldown so one pass doesn't fire twice. With these you silence, say, a car on the road in the corner of the frame while keeping people at the entrance.

## The motion gate — and why a person passes

Parking, entry and interest zones by default **do not fire on stationary objects**: a parked car's box jitter across a polygon edge would otherwise produce an endless enter/exit stream. But for a **person this is disabled**: someone who stops inside an interest zone is not still by accident — that is loitering, the single most report-worthy thing a camera can see. Vehicles keep the gate, people don't.

## The raw-feed preview

The Zone editor deliberately shows **raw detections** (the detector's output, before the tracker) so you see what the model says while drawing and calibrating. What the pipeline already discards — a static phantom, a detection in an ignore zone — is drawn greyed and dashed, with a reason. Grey means "this goes no further", not a fault.

---

Related: [How BABA sees](/help/how-baba-sees), [Tuning and phantoms](/help/tuning).
