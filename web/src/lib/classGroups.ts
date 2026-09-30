// Canonical detection-class groups for the events + visual-search filters.
// One source for the grouping so the two pages can't drift; each carries the
// friendly i18n label key, the COCO class NAMES (events indexes by name) and
// the COCO numeric IDs (search indexes by id).
//
// The activity/sightings feed keeps its own narrower "pet"-style grouping on
// purpose (different context + i18n keys), so it is intentionally NOT sourced
// from here.
import type { MessageKey } from "$lib/i18n";

export type ClassGroup = {
  id: string;
  key: MessageKey;
  names: string[];
  ids: number[];
};

export const CLASS_GROUPS: ClassGroup[] = [
  { id: "person",  key: "events_class_person",  names: ["person"],                              ids: [0] },
  { id: "vehicle", key: "events_class_vehicle", names: ["car", "truck", "bus", "motorcycle"],   ids: [2, 3, 5, 7] },
  { id: "bicycle", key: "events_class_bicycle", names: ["bicycle"],                             ids: [1] },
  { id: "animal",  key: "events_class_animal",  names: ["bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe"], ids: [14, 15, 16, 17, 18, 19, 20, 21, 22, 23] },
];
