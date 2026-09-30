// Which build this tab runs, and whether a newer one is served: the kit's
// watcher over BABA's /version.

import { VersionWatch } from "$lib/kit";
import { api, type VersionInfo } from "$lib/api";

export const version = new VersionWatch<VersionInfo>(() => api.getVersion());
