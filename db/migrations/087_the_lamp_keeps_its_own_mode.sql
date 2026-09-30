-- Which mode a camera's lamp is armed in, so switching it off is reversible.
--
-- "On" is not one setting across this yard: west is armed in the vendor's AI
-- mode (3), the camera over the gate in its night mode (1) — read off both
-- devices 29.08. Writing one of those values to every camera would quietly
-- convert the other one's behaviour the first time the house turned its light
-- back on, and nothing would report it: the lamp lights either way.
--
-- Off is the value that loses the information — mode 0 says nothing about what
-- the camera was doing before — so the mode is remembered while it is still
-- readable, and put back when the light is armed again. NULL means we have not
-- seen this camera armed yet.
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS light_armed_mode smallint;

COMMENT ON COLUMN cameras.light_armed_mode IS
    'Vendor white-LED mode this camera is armed in (learned from the device, '
    'never chosen by us). Restored when the light is switched back on.';
