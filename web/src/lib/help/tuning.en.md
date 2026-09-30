Detectors in poor light and on small/distant objects will **confidently get it wrong**: a bench read as a truck at 0.65, a lamp as a person, a crate as a car. That is not a camera fault or a setting you got wrong — it is the nature of the model. BABA handles it **itself**, without you having to chase thresholds.

## The self-learning phantom registry

BABA remembers where tracks are *born* and whether anything born there ever moved. A spot is suppressed only when it meets three conditions: enough births, **zero movers** (nothing ever left it), and a span longer than a day. That separates inventory from people — a lamp is there for days and never walks; a person who enters and stands will produce a few births, but over minutes, not days.

A spot is matched by the **foot**, not the box size — so the same lamp drawn with a narrower box still lands on the learned spot. And it has a hard safeguard: **a confident detection is never suppressed**. Once a track crosses the real-person bar even once, it is real for the rest of its life — a lamp never reaches that bar, a person does. That is why a person who sits down exactly where furniture usually stands still gets through.

It all **self-heals**: the instant anything moves off a suppressed spot, it becomes visible at once and that spot never suppresses again. Move the hydrant, and the spot ages out and disappears.

## When to touch a threshold

Settings → Detection holds the pipeline thresholds. **Default: leave them alone.** They are set to measured values, and a change that fixes one situation often breaks another.

When you do change one, it **applies immediately**, without a restart, and across the whole pipeline (no parallel tweaks visible in only one part). An empty field means "the deployment default applies". A value out of range is clamped — the thresholds are bounded because some of them don't loosen but *invert* a safeguard if pushed too far.

## Two tracking thresholds

A track is born only above the birth threshold, but maintained at a lower one. So a person who settles and whose confidence drops stays tracked — the track doesn't die on every weak frame. It is one of the mechanisms that keep a stationary person alive.

---

Related: [Zones](/help/zones), [How BABA sees](/help/how-baba-sees).
