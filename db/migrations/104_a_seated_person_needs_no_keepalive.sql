-- The event-manager no longer re-asserts zone_dwell every four minutes under a
-- seated person.
--
-- Those rows ({"hold": true}) kept a zone occupied for DIDA's 600 s stale
-- sweep. DIDA mirrors BABA's state snapshots and reads no event, so the
-- keepalive fed nobody; the Events feed and the notification dispatcher only
-- carried filters to hide it. Without the filters the old rows would surface
-- as loiter alerts, so they go.
DELETE FROM events WHERE kind = 'zone_dwell' AND payload ? 'hold';
