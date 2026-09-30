BABA is not an ordinary recorder. A classic NVR stores pixels and waits for a human to review them; BABA **understands the scene in real time** — it sees who arrived, follows them while they are present, recognises them when they return, and records that as events rather than hours of video.

Everything you see in the app comes out of one pipeline. It is worth understanding because it explains why something was recorded and something else was not.

## The pipeline

1. **Camera → decode.** Each camera provides an RTSP stream that BABA decodes once and shares that single frame with everything downstream.
2. **Detector.** On every frame a transformer model looks for objects — a person, a vehicle, an animal — and returns a box with a confidence. This is *per frame*: the detector does not know the person on this frame is the same as on the last.
3. **Tracker.** Links detections across frames into **tracks**: one person moving through the scene is one track, even when the detector briefly loses them. This is where "presence" is born.
4. **Recognition (re-ID).** For each track BABA computes an appearance vector of the body (and the face, if it sees one). That links the same subject across time and cameras — recognised when they return, and tomorrow, in a different jacket.
5. **Event manager.** Decides what is worth recording: entering a zone, lingering, a vehicle arriving, the end of a visit. This fills **Activity**.
6. **Recording.** Runs continuously 24/7, independent of detection; how much of it is kept is governed by the retention policy (see [Recording](/help/recording)). Activity is a layer of understanding on top of it.

## A detection is not a track

This is the distinction to remember. A **detection** is what the model sees on a single frame — raw, it can flicker, it can be fooled (a bench read as a person). A **track** is what the tracker assembles over time, and only that becomes a presence that is followed and recorded.

That is why the Zone editor shows raw detections (so you see what the model says while calibrating) and Activity shows visits (what survived). When the two differ it is not a fault — it means the pipeline discarded something, and BABA greys it out in the preview to show you.

## Why there is no motion detection

Most NVRs only start analysing when something moves. That has one fatal flaw: **it loses a person who stands still**. Someone who settles — sits, waits, watches — stops existing for such a system.

BABA deliberately does not work that way. The detector looks at every frame regardless of movement, and the tracker holds a person who settles by their appearance, not their motion. Reliably seeing a stationary person is a core requirement, not an add-on — and a whole chain of decisions in the system exists so that person is never lost.

---

Next: how presence becomes a record in [Activity and visits](/help/activity-and-visits); how it is decided what matters where in [Zones](/help/zones); how BABA knows who is who in [Identities and recognition](/help/identities).
