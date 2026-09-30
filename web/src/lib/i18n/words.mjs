#!/usr/bin/env node
// Every word the web says, checked rather than remembered — by the kit's
// checker, with the families only BABA knows. Half of them are vocabularies the
// backend owns, so it reads the repository, not just web/: `tests/run.sh`.

import { join } from 'node:path';
import { readFileSync } from 'node:fs';
import { checkWords, quotedIn, report } from '../kit/words/check.mjs';

// This file sits at <root>/web/src/lib/i18n/.
const ROOT = join(new URL('.', import.meta.url).pathname, '../../../..');
const TYPES = 'web/src/lib/api/types.ts';
const TUNABLES = 'core/src/baba_core/tunables.py';
const AUDIT = 'services/api/src/baba_api/audit.py';
const EVENT_KINDS = 'core/src/baba_core/event_kinds.py';
const field = (iface, name) => () =>
	quotedIn(ROOT, TYPES, new RegExp(`interface ${iface} \\{(?:(?!\\n\\})[\\s\\S])*?\\b${name}: ([^;]*);`));

const families = {
	users_role: { where: `BabaUser.role in ${TYPES}`, members: field('BabaUser', 'role') },
	fr_job_status: { where: `FaceRecomputeStatus.status in ${TYPES}`, members: field('FaceRecomputeStatus', 'status') },
	incident_status: { where: `TelemetryIncident.status in ${TYPES}`, members: field('TelemetryIncident', 'status') },
	sightings_suppressed: {
		where: `SuppressedTrack.suppressed_reason in ${TYPES}`,
		members: () => field('SuppressedTrack', 'suppressed_reason')().map((r) => r.replace('-', '_'))
	},
	events_range: {
		where: 'ACTIVITY_RANGES in web/src/lib/playback.svelte.ts',
		members: () => quotedIn(ROOT, 'web/src/lib/playback.svelte.ts', /ACTIVITY_RANGES = \[([^\]]*)\]/)
	},
	events_kind: {
		where: `EventKind in ${EVENT_KINDS}`,
		members: () => quotedIn(ROOT, EVENT_KINDS, /EventKind = Literal\[([^\]]*)\]/)
	},
	audit_resource: {
		where: `AuditResource in ${AUDIT}`,
		members: () => quotedIn(ROOT, AUDIT, /AuditResource = Literal\[([^\]]*)\]/)
	},
	audit_op: {
		where: `AuditOp in ${AUDIT}`,
		members: () => quotedIn(ROOT, AUDIT, /AuditOp = Literal\[([^\]]*)\]/)
	},
	tun_group: {
		where: `the *_KEY rows GROUPS is keyed by in ${TUNABLES}`,
		members: () => [...readFileSync(join(ROOT, TUNABLES), 'utf8').matchAll(/^\w+_KEY = "(\w+)"/gm)].map((m) => m[1])
	}
};

const named = [
	{
		where: `the Bound labels in ${TUNABLES}`,
		keys: () => [...readFileSync(join(ROOT, TUNABLES), 'utf8').matchAll(/Bound\([^)]*"(tun_\w+)"\)/g)].map((m) => m[1])
	}
];

report(checkWords({ root: ROOT, src: 'web/src', packages: ['web/src/lib/kit'], families, named, leftovers: true }));
