import type { IdentitySummary } from "$lib/api";
import { compareHr } from "$lib/kit";

export const IDENTITY_SECTIONS = ["person", "vehicle", "pet", "other"] as const;
export type IdentitySection = (typeof IDENTITY_SECTIONS)[number];

const AFFILIATIONS = ["family", "friend", "neighbour", "guest", "delivery", "service", "official", "unknown"];

/** Mirrors the server's re-ID class groups (VEHICLE_GROUP, PET_GROUP). */
export function sectionOfClass(className: string): IdentitySection {
  const c = className.toLowerCase();
  if (c === "person") return "person";
  if (c === "car" || c === "truck" || c === "bus" || c === "motorcycle") return "vehicle";
  if (c === "cat" || c === "dog") return "pet";
  return "other";
}

/** The operator's kind decides, the detector's class only when there is none —
 *  the rule the server's class filter applies, so a section never disagrees
 *  with its chip (the robot mower the detector calls a dog files as a device). */
export function sectionOf(id: IdentitySummary): IdentitySection {
  switch (id.label?.kind) {
    case "person": return "person";
    case "vehicle":
    case "object": return "vehicle";
    case "pet": return "pet";
    case "bird": return "other";
  }
  return sectionOfClass(id.class_name ?? "");
}

function affiliationRank(affiliation: string | undefined): number {
  const i = AFFILIATIONS.indexOf(affiliation ?? "unknown");
  return i < 0 ? AFFILIATIONS.length : i;
}

/** Named identities by what they are, then who lives here, then how close they
 *  are, then by name — an order that holds still as sightings come in. */
export function identitySections(named: readonly IdentitySummary[]): [IdentitySection, IdentitySummary[]][] {
  const sorted = [...named].sort((a, b) =>
    IDENTITY_SECTIONS.indexOf(sectionOf(a)) - IDENTITY_SECTIONS.indexOf(sectionOf(b))
    || Number(!!b.label?.resident) - Number(!!a.label?.resident)
    || affiliationRank(a.label?.affiliation) - affiliationRank(b.label?.affiliation)
    || compareHr(a.label?.name ?? "", b.label?.name ?? ""));
  const sections = new Map<IdentitySection, IdentitySummary[]>();
  for (const id of sorted) {
    const s = sectionOf(id);
    sections.set(s, [...(sections.get(s) ?? []), id]);
  }
  return [...sections.entries()];
}
