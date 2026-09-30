Recognition is what sets BABA apart from a recorder: the same person gets the same identity when they return — an hour later, tomorrow, in different clothes. It works through **appearance vectors** (embeddings): a numeric fingerprint that is close for the same person and far for others.

## Face and body — two signals

- **Face** is the strong signal. When BABA sees a face clearly enough, it decides: it links tracks across sessions and names the person.
- **Body** is the weaker, complementary signal. It recognises the same person by their whole appearance (OSNet body re-ID) even when the face isn't visible — hooded, from behind, in the dark.

The key rule: **the body alone must never reach a named identity.** Too many mistakes are possible (two people in a similar jacket). The body may name a person only when it attaches to a track a face has already confirmed — the *face-anchored body chain*. This recognises the same person on frames where their face isn't visible, without the risk of a similar appearance getting someone else's name.

## Naming and reference photos

You name a person by attaching reference photos. The more varied, named photos per person, the more reliable the recognition. BABA imports them from **OPUS · Library** (the family photo library; a library person becomes an identity in one step) or from **Immich** — either way it imports the **pixels, never someone else's vector**: each photo is re-embedded by BABA's own model so it lands in the same space as camera captures.

Pets are recognised **by body** (reference photos enrolled from stills); people are named **by face**; vehicles above all **by plate** (below).

## Vehicles: the plate is proof, appearance is an opinion

A vehicle is recognised poorly by appearance — "a dark estate car seen from above" describes half the street. So vehicles get a stronger signal: the **licence plate**, which BABA reads off the recording while the car moves up the drive. The plate is the only real proof of a vehicle's identity and therefore **beats every appearance-based conclusion** — even in reverse: a cleanly read plate that belongs to nobody enrolled *strips* a name appearance assigned wrongly.

Enter the plate on the vehicle's identity page and every subsequent arrival is named from the first read — guests included.

## Parking places

Draw a scene region carrying a place name (P1, P2…) and BABA keeps an **occupancy registry**: the place opens the moment a car stops and who is in it follows from where it stopped — named as soon as its plate is read, with its evidence (plate / appearance / undetermined). "Occupied — unknown vehicle" is a legitimate record: an honest blank beats a name nobody can defend. **One car holds at most one place**, and closed episodes give the history — who was parked, since when, for how long.

## Stays (people)

For named people BABA keeps **presence episodes**: who is at which camera, since when, until when. An episode opens only when the recognition *sustains* (one stray frame is not a person) and closes only when the person demonstrably left — seen elsewhere, the camera went person-free, or they left in their own car (a vehicle can be linked to its owner). A track's death never closes a stay: the tracker loses even a motionless person, and that is not a departure. On the identity page this is the **Stays** section.

## Species beats the detector

The detector is COCO-trained and will get the class wrong — a robot mower reads as "dog", a cat as "dog". So an identity carries a **species** as a separate axis: once a subject is tied to an identity, its species (dog/cat/vehicle/device) beats what the detector says on any given frame. Relabelling tracks wouldn't help — the next frame flips again; the identity is what knows what the subject *is*.

## The cost of licence cleanliness

Permissive embedders (the ones we may ship commercially) give a weak signal on surveillance faces in poor light. For private deployments this is solved with "bring your own model" (BYOM), where you supply a stronger model at your own risk. The face match threshold is set in Settings → Face recognition.

---

Related: [How BABA sees](/help/how-baba-sees), [Tuning](/help/tuning).
