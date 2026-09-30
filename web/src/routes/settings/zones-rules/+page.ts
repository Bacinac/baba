import { redirect } from "@sveltejs/kit";
import type { PageLoad } from "./$types";

// Zones/Rules is no longer a top-level settings section — it lives inside
// each camera (Settings → Kamere → [kamera] → "Zone i detekcija"). The old
// camera-picker list was a duplicate of the Cameras list, so send anyone
// landing here back to it.
export const load: PageLoad = () => {
  redirect(308, "/settings/cameras");
};
