Activity is a log of what happened, not a list of recordings. One row = **one visit**: a continuous presence of one subject on one camera. If the tracker loses and reacquires the same person, or the identity fragments across several tracks, BABA stitches them into one visit — you don't get five rows for one arrival.

## A vehicle: arrival and departure are two events

A car that parks in the morning and leaves in the evening is not one "4-hour visit" — it is two things that happened hours apart. So BABA shows them as **two rows, each at its own time**: an arrival at 04:52, a departure at 09:11. The departure surfaces fresh when it happens, instead of being buried on a row from the morning.

The departure carries a tag for **how long it was parked**, and each row's clip is a short bookend: arrival and parking (~40 s), then start-up and drive-off (~40 s). The dead middle — hours of a parked car sitting still — is neither a row nor a clip.

## A person: one continuous record

A person is the opposite case. Their still phase is **not dead time** — it is presence, and that is the whole point. So a person, however still they go, stays **one continuous record and one clip**, from arrival to departure. Splitting it would repeat the very failure BABA exists to avoid.

The arrival/departure split therefore applies to vehicles only: a parked car is genuinely inert, a settled person is not.

## Time in a zone

The card shows which zones a subject passed through and how long it spent in each. That is the **sum of actual enter→exit intervals**, not the span from first enter to last exit — otherwise a car that drives in through a gate, parks, and clips the gate again on the way out would read "in the gate for 4 hours".

The exception is a **parking zone**: a parked car emits no events while it sits, so for that kind BABA shows how long it was parked (the visit length), not the flickering sum.

## Activity vs. recording

Recording runs continuously — Activity is a layer above it. Clicking a row cuts a clip from the continuous recording for that moment. A dense band on the timeline does not mean "a flood of detections", only that footage exists there; what BABA *understood* is in the rows, not the band.

---

See also: [Zones](/help/zones), [Recording and storage](/help/recording).
