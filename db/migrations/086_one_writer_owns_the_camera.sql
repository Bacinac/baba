-- A temporary camera profile that outlives the process which applied it.
--
-- Two things now write to West: DIDA turns its light on when the house decides
-- it is dark, and BABA itself swaps the camera into an IR profile for the
-- twenty seconds a headlight is blinding the plate zone. Both go through the
-- api, so the device has ONE writer — but a temporary profile held only in that
-- process's memory is a promise it cannot keep. If the api is restarted or
-- killed mid-override, the camera stays in the temporary profile until somebody
-- notices in the morning, and the operator's own night view is gone with it.
--
-- So the baseline is written down before the override is applied, with the
-- moment it must be undone by. A row here means "a camera is not as its owner
-- left it": the sweep restores anything past its deadline, and startup restores
-- everything it finds, because a row surviving a restart is exactly the crash
-- this table exists for.
CREATE TABLE IF NOT EXISTS camera_profile_override (
    camera_id  uuid PRIMARY KEY REFERENCES cameras(id) ON DELETE CASCADE,
    -- The FULL settings object read off the device immediately before the
    -- override. Full, because the vendor API rejects a partial one, and read
    -- off the DEVICE rather than from our own idea of it: the camera can be
    -- changed from the vendor's app without telling us.
    baseline   jsonb       NOT NULL,
    reason     text        NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL
);

COMMENT ON TABLE camera_profile_override IS
    'Cameras currently held in a temporary profile, with what to put back and '
    'by when. Empty in steady state; a row that survives a restart is a crash '
    'to be repaired, not a state to be tolerated.';

CREATE INDEX IF NOT EXISTS camera_profile_override_expiry_idx
    ON camera_profile_override (expires_at);
