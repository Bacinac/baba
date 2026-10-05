import { describe, expect, it } from "vitest";
import type { IdentitySummary } from "$lib/api";
import { identitySections, sectionOf } from "./identities";

function named(name: string, kind: string | null, opts: { affiliation?: string; resident?: boolean; class_name?: string } = {}): IdentitySummary {
  return {
    global_id: `${kind}-${name}`,
    class_id: 0,
    class_name: opts.class_name ?? "person",
    n_tracks: 1,
    first_seen: "2026-10-01T00:00:00Z",
    last_seen: "2026-10-01T00:00:00Z",
    cameras: [],
    thumbnail_path: null,
    crop_path: null,
    nearest_dist: null,
    face_confirmed: false,
    label: {
      name, kind, tags: [], affiliation: opts.affiliation ?? "unknown", resident: opts.resident ?? false,
      species: null, plate: null, source: "manual", has_reference_embedding: false,
    },
  } as IdentitySummary;
}

describe("identitySections", () => {
  it("files people, vehicles, pets in chip order; household first, then closeness, then name", () => {
    const sections = identitySections([
      named("Zoran", "vehicle", { affiliation: "friend", class_name: "car" }),
      named("Mica", "pet", { class_name: "dog" }),
      named("Vesna", "person", { affiliation: "family" }),
      named("Bruno", "vehicle", { affiliation: "family", class_name: "car" }),
      named("Šime", "person", { affiliation: "family", resident: true }),
      named("Ana", "person", { affiliation: "family", resident: true }),
      named("Ante", "vehicle", { affiliation: "friend", class_name: "car" }),
      named("Fido", "pet", { class_name: "dog" }),
      named("Dostavljač", "person", { affiliation: "delivery" }),
    ]);
    expect(sections.map(([s, ids]) => [s, ids.map((i) => i.label!.name)])).toEqual([
      ["person", ["Ana", "Šime", "Vesna", "Dostavljač"]],
      ["vehicle", ["Bruno", "Ante", "Zoran"]],
      ["pet", ["Fido", "Mica"]],
    ]);
  });

  it("lets the operator's kind overrule the detector, and the detector decide only without one", () => {
    expect(sectionOf(named("Kosilica", "object", { class_name: "dog" }))).toBe("vehicle");
    expect(sectionOf(named("Fiat", null, { class_name: "truck" }))).toBe("vehicle");
    expect(sectionOf(named("Kos", "bird", { class_name: "bird" }))).toBe("other");
  });
});
