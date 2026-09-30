// Global live-view overlay toggles (app_settings.live_overlay), shared by the
// live grid, the per-camera detail view, and the Settings card that edits
// them. One fetch on first use; the Settings card writes through `save()` so
// open live views react without a reload.

import { api, type LiveOverlay } from "$lib/api";

class LiveOverlayStore {
  boxes = $state(true);
  badges = $state(true);
  #loaded = false;

  /** Fetch once. Never throws — falls back to both-on so a failed load still
   *  shows overlays (the historical behaviour). */
  async load(): Promise<void> {
    if (this.#loaded) return;
    this.#loaded = true;
    try {
      const v = await api.getLiveOverlay();
      this.boxes = v.boxes;
      this.badges = v.badges;
    } catch {
      /* keep defaults */
    }
  }

  async save(next: LiveOverlay): Promise<void> {
    const saved = await api.putLiveOverlay(next);
    this.boxes = saved.boxes;
    this.badges = saved.badges;
  }
}

export const liveOverlay = new LiveOverlayStore();
