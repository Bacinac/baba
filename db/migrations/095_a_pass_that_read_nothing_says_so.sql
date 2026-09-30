-- A crossing of the plate zone that yielded no plate left no trace, and the
-- sweep's only guard was the absence of a read — so every failed pass was
-- retried on the next tick, and the one after, for six hours: up to 240
-- decoded frames a minute, on an accelerator already at its ceiling, to
-- re-derive the same nothing.
--
-- The row is the attempt, not the result. Keyed the way the sweep groups its
-- events, because a pass IS several crossings a second apart.
CREATE TABLE plate_zone_pass_reads (
    camera_id    uuid        NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
    minute       timestamptz NOT NULL,
    attempted_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (camera_id, minute)
);
