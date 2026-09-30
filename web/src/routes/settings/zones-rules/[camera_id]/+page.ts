import { redirect } from "@sveltejs/kit";
import type { PageLoad } from "./$types";

// The per-camera zone editor was merged into the camera detail page
// (Settings → Kamere → [kamera] → "Zone i detekcija" tab). Keep old
// bookmarks / deep links alive by redirecting to that tab.
export const load: PageLoad = ({ params }) => {
  redirect(308, `/settings/cameras/${params.camera_id}?tab=zones`);
};
