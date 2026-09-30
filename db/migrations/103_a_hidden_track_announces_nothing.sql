-- A hidden track no longer writes a track_finalized event.
--
-- It did so for DIDA, which once kept its own zone members keyed by the
-- track's UUID and needed the finalize to release one before a 600 s stale
-- sweep did. DIDA now mirrors BABA's state snapshots and reads no event, so
-- nothing consumed these rows; what did see them was every reader of
-- track_finalized that does not know about the flag: a notification rule on
-- track_finalized with no class filter fires for a static car nobody is shown,
-- and the analytics breakdown counts it as a visit.
DELETE FROM events WHERE kind = 'track_finalized' AND payload ? 'suppressed';
