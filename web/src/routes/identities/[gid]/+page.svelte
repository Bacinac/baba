<script lang="ts">
  import { byLabel } from "$lib/order";
  import { onDestroy, onMount } from "svelte";
  import { page } from "$app/state";
  import { goto } from "$app/navigation";
  import {
    api, identitiesFeed, thumbnailUrl,
    IDENTITY_AUTO_MATCH_THRESHOLD,
    type IdentityAuditEvent, type IdentityDetail, type PresenceEpisode,
    type IdentityLabelIn, type IdentityReferencePhoto, type IdentitySummary,
    type OpusPerson, type ImmichPerson, type ReferenceSources,
    type Feed,
  } from "$lib/api";
  import OpusPeoplePicker from "$lib/OpusPeoplePicker.svelte";
  import { dialog, formatNumber, Button, Picks, Card, Tag, SaveButton } from "$lib/kit";
  import { t } from "$lib/i18n";
  import { formatUptime } from "$lib/format";
  import { dt } from "$lib/datetime.svelte";
  import { classLabel } from "$lib/classLabels";

  let gid = $derived(page.params.gid as string);
  let detail = $state<IdentityDetail | null>(null);
  let loading = $state(true);
  let error = $state<string | null>(null);
  let feed: Feed | null = null;
  let refreshTimer: ReturnType<typeof setTimeout> | null = null;

  // Merge picker
  let mergeOpen = $state(false);
  let mergeCandidates = $state<IdentitySummary[]>([]);
  let mergeLoading = $state(false);
  let mergeBusy = $state<string | null>(null);  // gid being merged in
  let mergeImgFailed = $state<Set<string>>(new Set());  // candidates whose crop file is gone → fall back to placeholder
  let mergeSelected = $state<Set<string>>(new Set());   // candidate gids ticked to fold into the current identity
  // "Ovo je…" — assign THIS anonymous cluster to an existing NAMED identity.
  // The merge picker above works from the named side ("pull anon clusters into
  // me"); this is the same merge from where the user actually browses — the
  // anon cluster with all the photos. Candidates are NAMED identities of the
  // same class, kNN-sorted by distance to this cluster (embeddings ARE the AI
  // suggestion — the likeliest match comes first with its distance shown).
  let assignOpen = $state(false);
  let assignCandidates = $state<IdentitySummary[]>([]);
  let assignLoading = $state(false);
  let assignBusy = $state(false);
  // Per-track split
  let splitBusy = $state<string | null>(null);  // track id being split
  // Bulk split selection (set of track ids)
  let selectedTracks = $state<Set<string>>(new Set());
  let bulkSplitProgress = $state<{ done: number; total: number } | null>(null);

  function toggleTrack(id: string) {
    const next = new Set(selectedTracks);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    selectedTracks = next;
  }

  function selectAll() {
    if (!detail) return;
    // Last track stays unselectable — splitting the last one is a no-op
    // (a singleton can't shed any more tracks). Matches the per-card UI.
    selectedTracks = new Set(detail.tracks.slice(0, -1).map(t => t.id));
  }

  function selectNone() {
    selectedTracks = new Set();
  }

  // --- label editing ---
  // Known kind enum (matches the add-identity modal and the server-side
  // CASE on `identity_labels.kind`). Empty string = "other / unset" →
  // serialised as null. Free-text values written by an older version of
  // the UI (or by AI) are preserved via a synthetic <option> rendered
  // below so the dropdown doesn't silently drop them on save.
  type KindKey = "" | "person" | "vehicle" | "pet" | "bird" | "object";
  const KIND_OPTIONS: { key: KindKey; label_key:
    "identities_add_kind_other" | "identities_add_kind_person"
    | "identities_add_kind_vehicle" | "identities_add_kind_pet"
    | "identities_add_kind_bird" | "identities_add_kind_object" }[] = [
    { key: "",        label_key: "identities_add_kind_other"   },
    { key: "person",  label_key: "identities_add_kind_person"  },
    { key: "vehicle", label_key: "identities_add_kind_vehicle" },
    { key: "pet",     label_key: "identities_add_kind_pet"     },
    { key: "bird",    label_key: "identities_add_kind_bird"    },
    // Devices the detector misreads as animals (the robot mower reads as a
    // dog). Display kind only — the MATCHING pool stays whatever
    // identity_labels.match_kind says (migration 066), so changing this
    // never silently breaks recognition.
    { key: "object",  label_key: "identities_add_kind_object"  },
  ];
  const STANDARD_KINDS = new Set(KIND_OPTIONS.map(k => k.key));
  // Relationship chips — mirrors the add modal. These used to write Croatian
  // strings into `tags` (multi-select) and drifted at once: one identity held
  // both `obitelj` and `family`, another only `obitelj`, so filtering "family"
  // missed it. They now set the structured `affiliation` column: single-select,
  // English in the DB, localised here, and valid for pets too (a neighbour's
  // cat is not ours). `resident` is a SEPARATE axis, not a value here — Nika
  // is family but no longer lives here.
  const AFFILIATION_CHIPS: { key: string; label_key:
    "identities_affiliation_family" | "identities_affiliation_friend"
    | "identities_affiliation_neighbour" | "identities_affiliation_guest"
    | "identities_affiliation_delivery" | "identities_affiliation_service"
    | "identities_affiliation_official" | "identities_affiliation_unknown" }[] = [
    { key: "family",    label_key: "identities_affiliation_family"    },
    { key: "friend",    label_key: "identities_affiliation_friend"    },
    { key: "neighbour", label_key: "identities_affiliation_neighbour" },
    { key: "guest",     label_key: "identities_affiliation_guest"     },
    { key: "delivery",  label_key: "identities_affiliation_delivery"  },
    { key: "service",   label_key: "identities_affiliation_service"   },
    { key: "official",  label_key: "identities_affiliation_official"  },
    { key: "unknown",   label_key: "identities_affiliation_unknown"   },
  ];

  let labelEditOpen = $state(false);
  let labelDraft = $state<IdentityLabelIn>({});
  let labelDraftTagsText = $state("");
  let labelDraftAffiliation = $state<string>("unknown");
  let labelDraftResident = $state(false);
  let labelDraftSpecies = $state("");
  let labelBusy = $state(false);
  let labelError = $state<string | null>(null);

  // Anything the AI / older UI / SQL might have put in `kind` that
  // isn't one of our standard options. Normalise common aliases
  // (e.g. "dog", "cat", "car") down to a standard key so the dropdown
  // works; everything else is preserved verbatim as a synthetic
  // <option> so save doesn't silently rewrite operator-chosen text.
  function normalizeKind(raw: string | null | undefined): string {
    const k = (raw ?? "").trim().toLowerCase();
    if (!k) return "";
    if (STANDARD_KINDS.has(k as KindKey)) return k;
    if (["dog", "cat", "pas", "mačka", "macka"].includes(k)) return "pet";
    if (["car", "truck", "bus", "motorcycle", "auto", "vozilo"].includes(k)) return "vehicle";
    if (["human", "osoba", "čovjek", "covjek"].includes(k)) return "person";
    // Unknown — keep verbatim so the synthetic <option> picks it up.
    return raw ?? "";
  }

  function openLabelEdit() {
    if (!detail) return;
    const l = detail.label;
    const allTags = l?.tags ?? [];
    labelDraft = {
      name: l?.name ?? "",
      kind: normalizeKind(l?.kind),
      tags: allTags,
      notes: l?.notes ?? "",
      plate: l?.plate ?? "",
      linked_person: l?.linked_person ?? null,
    };
    // Tags are free description only now — relationship/residency/species have
    // their own columns, so there is nothing to split out any more.
    labelDraftTagsText = allTags.join(", ");
    labelDraftAffiliation = l?.affiliation ?? "unknown";
    labelDraftResident = l?.resident ?? false;
    labelDraftSpecies = l?.species ?? "";
    labelError = null;
    labelStored = JSON.stringify(labelBody());
    labelEditOpen = true;
  }

  function labelBody() {
    // Parse the comma-separated tags input on submit so the user sees the
    // raw text while editing; trim + dedupe so accidental spaces don't
    // create distinct "watch" vs " watch" tags. Tags are free description
    // ONLY — relationship/residency/species are structured columns.
    const tags = Array.from(new Set(
      labelDraftTagsText.split(",").map(t => t.trim()).filter(Boolean)
    ));
    // Plate only makes sense for vehicles; null it out on save when
    // the operator switches kind away from vehicle so a stale plate
    // doesn't keep showing on a pet's badge.
    const plate = labelDraft.kind === "vehicle"
      ? ((labelDraft.plate ?? "").trim() || null)
      : null;
    return {
      name: (labelDraft.name ?? "").trim() || undefined,
      kind: (labelDraft.kind ?? "")?.trim() || null,
      tags,
      affiliation: labelDraftAffiliation,
      resident: labelDraftResident,
      // Species is a pet fact; don't persist a stale one if the operator
      // switched kind away from pet.
      species: labelDraft.kind === "pet"
        ? (labelDraftSpecies.trim().toLowerCase() || null)
        : null,
      notes: (labelDraft.notes ?? "")?.trim() || null,
      plate,
      linked_person: labelDraft.kind === "vehicle" ? (labelDraft.linked_person ?? null) : null,
    };
  }

  let labelStored = $state("");
  const labelDirty = $derived(labelEditOpen && JSON.stringify(labelBody()) !== labelStored);

  async function saveLabel() {
    if (!detail || labelBusy) return;
    labelBusy = true;
    labelError = null;
    try {
      await api.upsertIdentityLabel(detail.global_id, labelBody());
      labelEditOpen = false;
      await load(true);
    } catch (e) {
      labelError = (e as Error).message;
    } finally {
      labelBusy = false;
    }
  }

  // --- ask AI ---
  let aiBusy = $state(false);
  let aiError = $state<string | null>(null);
  let aiRaw = $state<string | null>(null);
  let aiHint = $state<string | null>(null);  // "AI confidence: medium · last result: …"

  async function askAi() {
    if (!detail || aiBusy) return;
    aiBusy = true;
    aiError = null;
    aiRaw = null;
    try {
      const r = await api.describeIdentity(detail.global_id);
      if (!r.ok) {
        aiError = r.error;
        aiRaw = r.raw;
        return;
      }
      const s = r.suggestion;
      // Fill the form fields the AI returned, but don't overwrite a
      // value the user has already typed — they may have started a
      // manual label and just want AI to fill in the rest.
      if (!labelDraft.name && s.name) labelDraft.name = s.name;
      // AI may return a free-form kind like "dog" or "delivery van".
      // Snap to our standard enum where possible so the dropdown picks
      // it up; otherwise the synthetic option preserves the raw text.
      if (!labelDraft.kind && s.kind) labelDraft.kind = normalizeKind(s.kind);
      // The model is asked for `species` separately and is never asked to
      // guess affiliation/residency — it cannot know that from an image.
      if (!labelDraftSpecies.trim() && s.species) labelDraftSpecies = String(s.species);
      if (!labelDraftTagsText.trim() && Array.isArray(s.tags) && s.tags.length > 0) {
        labelDraftTagsText = s.tags.join(", ");
      }
      if (!labelDraft.notes && s.description) labelDraft.notes = s.description;
      if (!labelDraft.plate && s.plate) labelDraft.plate = s.plate;
      aiHint = s.confidence ? t("identities_ai_confidence").replace("{c}", String(s.confidence)) : null;
    } catch (e) {
      aiError = (e as Error).message;
    } finally {
      aiBusy = false;
    }
  }

  async function deleteLabel() {
    if (!detail) return;
    const ok = await dialog.confirm({
      title: t("identities_label_delete"),
      message: t("identities_label_delete_confirm"),
      confirmLabel: t("dialog_delete"),
      danger: true,
    });
    if (!ok) return;
    try {
      await api.deleteIdentityLabel(detail.global_id);
      labelEditOpen = false;
      await load(true);
    } catch (e) {
      labelError = (e as Error).message;
    }
  }

  // Full purge of the whole identity (tracks, samples, events, label,
  // photos, files). Distinct from `onDeleteLabel` which only strips
  // the operator-set label and leaves the cluster intact.
  let deleteBusy = $state(false);
  async function onDeleteIdentity() {
    if (!detail) return;
    const msg = detail.label
      ? t("identities_delete_confirm").replace("{name}", detail.label.name)
      : t("identities_delete_confirm_anon");
    const ok = await dialog.confirm({
      title: t("identities_delete_confirm_title"),
      message: msg,
      confirmLabel: t("dialog_delete"),
      danger: true,
    });
    if (!ok) return;
    deleteBusy = true;
    try {
      await api.deleteIdentity(detail.global_id);
      void goto("/identities");
    } catch (e) {
      deleteBusy = false;
      await dialog.alert({
        title: t("dialog_error_title"),
        message: (e as Error).message,
      });
    }
  }

  // --- reference photos ---
  let refsUploading = $state(false);
  let refsLastResult = $state<{ used: number; skipped: string[] } | null>(null);
  let refsError = $state<string | null>(null);
  let refsList = $state<IdentityReferencePhoto[]>([]);
  let refsListLoading = $state(false);
  let refsDeleting = $state<string | null>(null);  // id of photo being deleted

  async function loadReferencePhotos() {
    if (!detail) return;
    refsListLoading = true;
    try {
      refsList = await api.listReferencePhotos(detail.global_id);
    } catch (e) {
      refsError = (e as Error).message;
    } finally {
      refsListLoading = false;
    }
  }

  async function deleteReferencePhoto(p: IdentityReferencePhoto) {
    if (!detail || refsDeleting) return;
    const ok = await dialog.confirm({
      title: t("identities_refs_delete"),
      message: t("identities_refs_delete_confirm"),
      confirmLabel: t("dialog_delete"),
      danger: true,
    });
    if (!ok) return;
    refsDeleting = p.id;
    refsError = null;
    try {
      await api.deleteReferencePhoto(detail.global_id, p.id);
      await loadReferencePhotos();
      await load(true);  // reference_count + has_reference_embedding in label
    } catch (e) {
      refsError = (e as Error).message;
    } finally {
      refsDeleting = null;
    }
  }

  // Reload the photo list when the IDENTITY changes (global_id VALUE), not on
  // every `detail` reassignment — a $derived only propagates on value change,
  // so load(true) after an edit/delete (same global_id) no longer spuriously
  // refetches the photos a second time on top of the explicit reload.
  let refsGid = $derived(detail?.global_id);
  $effect(() => { void refsGid; void loadReferencePhotos(); });

  async function onPhotoFiles(e: Event) {
    if (!detail) return;
    const target = e.currentTarget as HTMLInputElement;
    const files = Array.from(target.files ?? []);
    if (files.length === 0) return;
    refsUploading = true;
    refsError = null;
    try {
      const r = await api.uploadReferencePhotos(detail.global_id, files);
      refsLastResult = { used: r.used, skipped: r.skipped };
      await load(true);
      await loadReferencePhotos();
    } catch (e) {
      refsError = (e as Error).message;
    } finally {
      refsUploading = false;
      target.value = "";
    }
  }

  // --- cover photo override ---
  // Operator pins a specific crop as the identity's "face card" via
  // the small button on each gallery thumbnail. Stored on
  // identity_labels.cover_photo_path and consumed by the detail/list
  // endpoints in place of the auto-picked latest_crop. Comparing
  // against detail.label?.cover_photo_path tells the gallery which
  // thumbnail currently owns the cover so we can render the badge +
  // disable its button.
  let coverBusy = $state(false);
  async function setCoverPhoto(photoPath: string | null) {
    if (!detail || coverBusy) return;
    coverBusy = true;
    try {
      await api.setIdentityCoverPhoto(detail.global_id, photoPath);
      await load(true);
    } catch (e) {
      await dialog.alert({
        title: t("dialog_error_title"),
        message: t("identities_cover_error")
          .replace("{msg}", (e as Error).message),
      });
    } finally {
      coverBusy = false;
    }
  }

  // --- face badge relevance ---
  // The face cascade (yunet + AuraFace) is trained on humans only.
  // For pet/vehicle/bird/other identities it will always return
  // "no face", which is structurally guaranteed — showing a "— no face"
  // badge on Lumi's reference photos implies a failed detection when
  // really the cascade was never going to succeed. Hide the badge
  // whenever the operator-set kind explicitly says this isn't a
  // person; keep showing it for kind=person or unlabeled (where
  // face detection is the meaningful signal).
  function faceBadgeRelevant(kind: string | null | undefined): boolean {
    if (!kind) return true;
    return kind.toLowerCase() === "person";
  }

  // --- badge logic: hide redundant detector class chips ---
  // The detector flips class within a class-group (cat↔dog,
  // car↔truck↔bus↔motorcycle) on the same physical subject. When the
  // operator has set kind=pet, showing both "cat" AND "pet" chips on
  // a dog's identity is noise. Hide the detector chip when the
  // operator-set kind explicitly covers the detected class. Mirrors
  // the server-side PET_GROUP / VEHICLE_GROUP grouping used for re-ID.
  const PET_CLASS_NAMES = new Set(["cat", "dog"]);
  const VEHICLE_CLASS_NAMES = new Set(["car", "truck", "bus", "motorcycle"]);
  // The identity owns what an animal IS. The detector flips cat<->dog on the
  // same animal — 19 of Franka's 21 tracks say "dog" — which is precisely why
  // cat+dog are pooled into PET_GROUP for re-ID: the embedding decides identity,
  // the per-frame label can't be trusted. So once a track is matched here, the
  // identity's species wins over class_name. Relabelling tracks would fix
  // nothing; the next one flips again.
  function displayClass(className: string | null | undefined): string {
    // Stored as a lowercased English token; the line below already routes
    // the detector class through the translated dictionary, so returning this
    // one raw put English on a Croatian card.
    if (detail?.label?.species) return classLabel(detail.label.species);
    // A device's detector class is a known misread — show what it IS.
    if (detail?.label?.kind?.toLowerCase() === "object") return kindLabel("object");
    return classLabel(className);
  }

  function classChipRedundant(kind: string | null | undefined, className: string | null | undefined): boolean {
    if (!kind || !className) return false;
    const k = kind.toLowerCase();
    const c = className.toLowerCase();
    if (k === "pet" && PET_CLASS_NAMES.has(c)) return true;
    if (k === "vehicle" && VEHICLE_CLASS_NAMES.has(c)) return true;
    if (k === "bird" && c === "bird") return true;
    if (k === "person" && c === "person") return true;
    // A DEVICE has no meaningful detector class at all — the class is a known
    // misread (the robot mower reads as "dog"; that's exactly why the operator
    // marked it a device). Never show it.
    if (k === "object") return true;
    return false;
  }

  // Kind chip display: translated label for the standard kinds, raw value for
  // anything custom — a chip reading "object" on a formal-Croatian UI is a
  // vocabulary leak.
  function kindLabel(kind: string): string {
    const opt = KIND_OPTIONS.find((o) => o.key === kind.toLowerCase());
    return opt ? t(opt.label_key) : kind;
  }

  // Server-side auto-selection: ask the backend to pick the most
  // visually varied crops from existing tracks (farthest-point sampling
  // over body embeddings). Bootstrap for identities with many tracks
  // but no curated reference set yet.
  const AUTO_PICK_COUNT = 8;
  let refsAutoBusy = $state(false);
  async function autoPickReferences() {
    if (!detail || refsAutoBusy) return;
    refsAutoBusy = true;
    refsError = null;
    try {
      const r = await api.referencePhotosAutoSelect(
        detail.global_id, { count: AUTO_PICK_COUNT },
      );
      refsLastResult = { used: r.used, skipped: r.skipped };
      await load(true);
      await loadReferencePhotos();
    } catch (e) {
      refsError = (e as Error).message;
    } finally {
      refsAutoBusy = false;
    }
  }

  // Enrol the identity's own CCTV faces. Every face reference here comes from
  // an imported portrait library, so every comparison the matcher makes is
  // surveillance-crop against studio-portrait. This adds references from the
  // same domain the cameras produce, taken from tracks a face match already
  // confirmed. The backend's face-size floor is what keeps it safe — measured,
  // enrolling small faces raises recall slightly but turns them into
  // attractors for anyone at that distance.
  const FACE_SAMPLES_COUNT = 8;
  let refsFaceBusy = $state(false);
  async function enrolFaceSamples() {
    if (!detail || refsFaceBusy) return;
    refsFaceBusy = true;
    refsError = null;
    try {
      const r = await api.referencePhotosFromFaceSamples(
        detail.global_id, { count: FACE_SAMPLES_COUNT },
      );
      refsLastResult = { used: r.used, skipped: r.skipped };
      await load(true);
      await loadReferencePhotos();
    } catch (e) {
      refsError = (e as Error).message;
    } finally {
      refsFaceBusy = false;
    }
  }

  // --- purge references that carry no face ---
  // A reference photo without a face buys nothing: face is the only
  // cross-session identity signal here. It still counts toward
  // reference_count, so the enrollment reads stronger than it is.
  const refsFacelessCount = $derived(refsList.filter((p) => !p.has_face).length);
  // Face references are what recognition runs on; body-only ones (legacy rows,
  // and pets/vehicles which have no face at all) are listed apart so a set
  // cannot look well-stocked while carrying almost no face evidence.
  // One photo carries up to two claims, so it can appear in both groups: the
  // face group shows it zoomed to the box the face matcher actually consumed,
  // the body group shows the same picture whole, which is what OSNet saw.
  const refsFace = $derived(refsList.filter((p) => p.has_face));
  const refsBody = $derived(refsList.filter((p) => p.has_body));

  // The stored photo is the whole crop, so a tile of a torso with a head in
  // the corner looked identical to a portrait. Zooming to the recorded box is
  // the only way the operator can see what the matcher was actually given.
  function faceZoom(node: HTMLImageElement, bbox: number[] | null) {
    const apply = (box: number[] | null) => {
      if (!box || box.length !== 4) {
        node.removeAttribute("style");
        return;
      }
      const W = node.naturalWidth;
      const H = node.naturalHeight;
      if (!W || !H) return;
      const x1 = box[0] * W;
      const y1 = box[1] * H;
      const x2 = box[2] * W;
      const y2 = box[3] * H;
      // Square window around the box keeps the face un-stretched whatever the
      // photo's aspect; 1.6x leaves enough hair and chin to recognise it.
      const side = Math.max(x2 - x1, y2 - y1, 1) * 1.6;
      const cx = (x1 + x2) / 2;
      const cy = (y1 + y2) / 2;
      node.style.position = "absolute";
      node.style.maxWidth = "none";
      node.style.objectFit = "fill";
      node.style.width = `${(W / side) * 100}%`;
      node.style.height = `${(H / side) * 100}%`;
      node.style.left = `${(-(cx - side / 2) / side) * 100}%`;
      node.style.top = `${(-(cy - side / 2) / side) * 100}%`;
    };
    let current = bbox;
    const onLoad = () => apply(current);
    node.addEventListener("load", onLoad);
    if (node.complete) apply(current);
    return {
      update(next: number[] | null) {
        current = next;
        apply(next);
      },
      destroy() {
        node.removeEventListener("load", onLoad);
      },
    };
  }
  let refsPurgeBusy = $state(false);

  async function purgeFacelessReferences() {
    if (!detail || refsPurgeBusy) return;
    const ok = await dialog.confirm({
      title: t("identities_refs_purge_faceless"),
      message: t("identities_refs_purge_faceless_confirm")
        .replace("{n}", String(refsFacelessCount)),
      confirmLabel: t("dialog_delete"),
      danger: true,
    });
    if (!ok) return;
    refsPurgeBusy = true;
    refsError = null;
    try {
      await api.deleteFacelessReferencePhotos(detail.global_id);
      refsLastResult = null;
      await load(true);
      await loadReferencePhotos();
    } catch (e) {
      refsError = (e as Error).message;
    } finally {
      refsPurgeBusy = false;
    }
  }

  // --- import from OPUS · Library ---
  // Pulls the pixels of a face the family photo library already located and
  // named, re-embedded here by BABA's own model. The lever is reference
  // VARIETY: matching is MIN-over-references, and the library holds dozens of
  // varied shots per person where an identity here typically has one or two.
  // Face-only — a portrait carries no useful body vector. A linked identity
  // (label.opus_person_id) needs no picking.
  const OPUS_IMPORT_COUNT = 32;
  // Which external libraries are set up; the import doors shown are theirs.
  let sources = $state<ReferenceSources | null>(null);
  onMount(async () => {
    try { sources = await api.referenceSources(); } catch { sources = null; }
  });

  function refSource(source: string): string {
    switch (source) {
      case "opus": return "OPUS";
      case "immich": return "Immich";
      case "from-tracks":
      case "auto-select":
      case "from-face-samples": return t("identities_refs_source_camera");
      default: return t("identities_refs_source_upload");
    }
  }
  let opusOpen = $state(false);
  let opusBusy = $state(false);

  function openOpus() {
    if (!detail) return;
    if (detail.label?.opus_person_id) {
      void importFromOpus(null);
      return;
    }
    opusOpen = true;
    refsError = null;
  }

  async function importFromOpus(p: OpusPerson | null) {
    if (!detail || opusBusy) return;
    if (p) {
      const ok = await dialog.confirm({
        title: t("identities_refs_opus"),
        message: t("identities_refs_opus_confirm")
          .replace("{person}", p.name)
          .replace("{name}", detail.label?.name ?? ""),
        confirmLabel: t("identities_refs_opus_do"),
      });
      if (!ok) return;
    }
    opusBusy = true;
    refsError = null;
    try {
      const r = await api.referencePhotosFromOpus(detail.global_id, {
        person_id: p?.id ?? null,
        limit: OPUS_IMPORT_COUNT,
      });
      refsLastResult = { used: r.used, skipped: r.skipped };
      opusOpen = false;
      await load(true);
      await loadReferencePhotos();
    } catch (e) {
      refsError = (e as Error).message;
    } finally {
      opusBusy = false;
    }
  }

  // --- import from Immich ---
  // Pulls the pixels of a face Immich already located and named, re-embedded
  // here by BABA's own model. The lever is reference VARIETY: matching is
  // MIN-over-references, and a photo library holds dozens of varied shots per
  // person where an identity here typically has one or two. Face-only — a
  // portrait carries no useful body vector.
  const IMMICH_IMPORT_COUNT = 32;
  let immichOpen = $state(false);
  let immichLoading = $state(false);
  let immichConfigured = $state(true);
  let immichPeople = $state<ImmichPerson[]>([]);
  let immichFilter = $state("");
  let immichBusy = $state(false);

  const immichMatches = $derived(
    immichFilter.trim()
      ? immichPeople.filter((p) =>
          p.name.toLowerCase().includes(immichFilter.trim().toLowerCase()),
        )
      : immichPeople,
  );

  async function openImmichPicker() {
    if (!detail) return;
    immichOpen = true;
    immichLoading = true;
    refsError = null;
    // Seed the filter with this identity's name — the overwhelmingly common
    // case is enrolling the same person we're already looking at.
    immichFilter = detail.label?.name ?? "";
    try {
      const status = await api.immichStatus();
      immichConfigured = status.configured;
      if (!status.configured) return;
      immichPeople = (await api.immichPeople()).people;
    } catch (e) {
      refsError = (e as Error).message;
      immichOpen = false;
    } finally {
      immichLoading = false;
    }
  }

  async function importFromImmich(p: ImmichPerson) {
    if (!detail || immichBusy) return;
    const ok = await dialog.confirm({
      title: t("identities_refs_immich"),
      message: t("identities_refs_immich_confirm")
        .replace("{person}", p.name)
        .replace("{name}", detail.label?.name ?? ""),
      confirmLabel: t("identities_refs_immich_do"),
    });
    if (!ok) return;
    immichBusy = true;
    refsError = null;
    try {
      const r = await api.referencePhotosFromImmich(detail.global_id, {
        person_id: p.id,
        limit: IMMICH_IMPORT_COUNT,
      });
      refsLastResult = { used: r.used, skipped: r.skipped };
      immichOpen = false;
      await load(true);
      await loadReferencePhotos();
    } catch (e) {
      refsError = (e as Error).message;
    } finally {
      immichBusy = false;
    }
  }

  // Promote selected track crops into reference photos. Reuses
  // `selectedTracks` (shared with bulk split) — both actions are
  // multi-select operations on the same track list, so a single
  // selection set keeps the UI predictable.
  let refsPromoteBusy = $state(false);
  async function promoteSelectedTracks() {
    if (!detail || refsPromoteBusy || selectedTracks.size === 0) return;
    refsPromoteBusy = true;
    refsError = null;
    try {
      const ids = Array.from(selectedTracks);
      const r = await api.referencePhotosFromTracks(detail.global_id, ids);
      refsLastResult = { used: r.used, skipped: r.skipped };
      selectedTracks = new Set();
      await load(true);
      await loadReferencePhotos();
    } catch (e) {
      refsError = t("identities_refs_promote_error")
        .replace("{msg}", (e as Error).message);
    } finally {
      refsPromoteBusy = false;
    }
  }

  // Hard-delete selected sightings (row + crops). For pruning redundant
  // near-duplicate shots that won't promote to references — the operator
  // already has a representative reference, the extra crops are just clutter.
  let deleteSightingsBusy = $state(false);
  async function deleteSelectedSightings() {
    if (!detail || deleteSightingsBusy || selectedTracks.size === 0) return;
    const n = selectedTracks.size;
    const ok = await dialog.confirm({
      title: t("identities_delete_sightings_title"),
      message: t("identities_delete_sightings_confirm").replace("{n}", String(n)),
      confirmLabel: t("dialog_delete"),
      danger: true,
    });
    if (!ok) return;
    deleteSightingsBusy = true;
    error = null;
    try {
      await api.deleteSightings(detail.global_id, Array.from(selectedTracks));
      selectedTracks = new Set();
      await load(true);
    } catch (e) {
      error = (e as Error).message;
    } finally {
      deleteSightingsBusy = false;
    }
  }

  // Per-row variant of the same hard delete — one click for the common case
  // (a redundant near-duplicate) without the checkbox-then-header dance.
  async function deleteOneSighting(trackId: string) {
    if (!detail || deleteSightingsBusy) return;
    const ok = await dialog.confirm({
      title: t("identities_delete_sightings_title"),
      message: t("identities_delete_sightings_confirm").replace("{n}", "1"),
      confirmLabel: t("dialog_delete"),
      danger: true,
    });
    if (!ok) return;
    deleteSightingsBusy = true;
    error = null;
    try {
      await api.deleteSightings(detail.global_id, [trackId]);
      await load(true);
    } catch (e) {
      error = (e as Error).message;
    } finally {
      deleteSightingsBusy = false;
    }
  }

  // Monotonic guard so a fast gid1→gid2 navigation can't render gid1's
  // response over gid2 if it resolves last (same pattern as the list page).
  let loadSeq = 0;

  // Presence episodes — the registry's stays, shown only for persons (pets and
  // vehicles have no presence episodes by design). Fetched alongside detail;
  // a failure keeps the section empty rather than failing the page.
  let presence = $state<PresenceEpisode[]>([]);
  // Person identities for the vehicle "owner" select; fetched lazily when the
  // label editor opens on a vehicle.
  let personOptions = $state<{ gid: string; name: string }[]>([]);
  async function loadPersonOptions() {
    if (personOptions.length) return;
    try {
      const all = await api.listIdentities({ labeled: true });
      personOptions = byLabel(
        all
          .filter((i) => i.label?.kind === "person" && i.label?.name)
          .map((i) => ({ gid: i.global_id, name: i.label!.name })),
        (p) => p.name,
      );
    } catch { /* select stays empty; saving without it is fine */ }
  }

  async function load(silent = false) {
    const myseq = ++loadSeq;
    if (!silent) loading = true;
    error = null;
    try {
      const [d, pe] = await Promise.all([
        api.getIdentity(gid),
        api.getIdentityPresence(gid).catch(() => [] as PresenceEpisode[]),
      ]);
      if (myseq !== loadSeq) return; // superseded by a newer load
      detail = d;
      presence = pe;
    } catch (e) {
      if (myseq === loadSeq) error = (e as Error).message;
    } finally {
      if (!silent && myseq === loadSeq) loading = false;
    }
  }

  function onAuditEvent(ev: IdentityAuditEvent) {
    // Only refetch on events affecting *this* identity. Each NOTIFY payload
    // carries `payload.gid` (the surviving identity for merge, the source
    // for split, the matched identity for auto_match), so filter on that.
    if (ev.payload?.gid === gid) scheduleRefresh();
  }

  function scheduleRefresh() {
    if (refreshTimer !== null) clearTimeout(refreshTimer);
    refreshTimer = setTimeout(() => {
      refreshTimer = null;
      void load(true);
    }, 1000);
  }

  async function openMergePicker() {
    if (!detail) return;
    mergeOpen = true;
    mergeLoading = true;
    mergeSelected = new Set();
    mergeImgFailed = new Set();
    try {
      // Candidates = UNNAMED sightings that have a crop, kNN-sorted by
      // cosine distance to this identity (`near`). We deliberately exclude
      // other NAMED identities (`labeled: false`): the task here is "grow
      // this person by pulling in their unnamed sightings", not "merge two
      // named people" — and surfacing other portraits as 'likely match'
      // (Ema ≈ Ana ≈ Nika at 0.3) was pure noise. has_image hides the
      // un-reviewable "no thumbnail" clusters. Same-class throughout.
      mergeCandidates = await api.listIdentities({
        class_id: detail.class_id,
        near: detail.global_id,
        labeled: false,
        has_image: true,
        limit: 200,
      });
    } catch (e) {
      error = (e as Error).message;
    } finally {
      mergeLoading = false;
    }
  }

  async function openAssignPicker() {
    if (!detail) return;
    assignOpen = true;
    assignLoading = true;
    try {
      assignCandidates = await api.listIdentities({
        class_id: detail.class_id,
        near: detail.global_id,
        labeled: true,
        limit: 24,
      });
    } catch (e) {
      error = (e as Error).message;
    } finally {
      assignLoading = false;
    }
  }

  async function doAssign(target: IdentitySummary) {
    if (!detail || assignBusy) return;
    const name = target.label?.name ?? "?";
    const ok = await dialog.confirm({
      title: t("identities_assign_title"),
      message: t("identities_assign_confirm").replace("{name}", name),
    });
    if (!ok) return;
    assignBusy = true;
    try {
      // THIS cluster folds into the chosen named identity — its tracks and
      // photos move there; this gid ceases to exist. Land on the survivor.
      await api.mergeIdentity(detail.global_id, target.global_id);
      await goto(`/identities/${target.global_id}`);
    } catch (e) {
      error = (e as Error).message;
      assignBusy = false;
    }
  }

  function toggleMergeSelect(gid: string) {
    const n = new Set(mergeSelected);
    if (n.has(gid)) n.delete(gid);
    else n.add(gid);
    mergeSelected = n;
  }

  async function doMergeSelected() {
    if (!detail || mergeBusy) return;
    const into = detail.global_id;
    const sources = [...mergeSelected].filter((g) => g !== into);
    if (sources.length === 0) return;
    const ok = await dialog.confirm({
      title: t("identities_merge_button"),
      message: t("identities_merge_confirm"),
    });
    if (!ok) return;
    try {
      // Fold every selected identity into the one we're looking at — the
      // current page is the survivor and keeps its label. Sequential so a
      // failure stops cleanly.
      for (const src of sources) {
        mergeBusy = src;
        await api.mergeIdentity(src, into);
      }
      mergeOpen = false;
      mergeSelected = new Set();
      await load();
    } catch (e) {
      error = (e as Error).message;
    } finally {
      mergeBusy = null;
    }
  }

  async function doSplit(trackId: string) {
    if (!detail || splitBusy) return;
    const ok = await dialog.confirm({
      title: t("identities_split_button"),
      message: t("identities_split_confirm"),
    });
    if (!ok) return;
    splitBusy = trackId;
    try {
      await api.splitIdentity(detail.global_id, trackId);
      await load();
    } catch (e) {
      error = (e as Error).message;
    } finally {
      splitBusy = null;
    }
  }

  async function doBulkSplit() {
    if (!detail || bulkSplitProgress) return;
    const ids = Array.from(selectedTracks);
    if (ids.length === 0) return;
    const msg = t("identities_split_selected_confirm").replace("{n}", String(ids.length));
    const ok = await dialog.confirm({
      title: t("identities_split_button"),
      message: msg,
    });
    if (!ok) return;
    bulkSplitProgress = { done: 0, total: ids.length };
    try {
      // Sequential rather than Promise.all so progress is meaningful and
      // a partial failure can be reported cleanly with what was done.
      for (const id of ids) {
        await api.splitIdentity(detail.global_id, id);
        bulkSplitProgress = { done: bulkSplitProgress.done + 1, total: ids.length };
      }
      selectedTracks = new Set();
      await load();
    } catch (e) {
      error = (e as Error).message;
    } finally {
      bulkSplitProgress = null;
    }
  }

  // Re-fetch when navigating between identities without a full page reload.
  $effect(() => { void gid; void load(); });

  onMount(() => {
    feed = identitiesFeed({ message: onAuditEvent, resync: scheduleRefresh });
  });

  onDestroy(() => {
    if (refreshTimer !== null) clearTimeout(refreshTimer);
    feed?.close();
  });
</script>

<div class="mb-4">
  <a href="/identities" class="text-s text-baba-text-faint hover:text-baba-text-muted">
    {t("identities_back")}
  </a>
</div>

{#if loading}
  <p class="text-baba-text-faint">{t("identities_loading")}</p>
{:else if error || !detail}
  <p class="text-red-400">{error ?? "not found"}</p>
{:else}
  <!-- One focused thumb on the left (square, sized so it scales with
       the row's intrinsic height). Right column uses flex-col +
       justify-between so its top (badges + name + tags) and bottom
       (dates + buttons) hug the top and bottom of the same visual row
       — visually equal-height with the photo. -->
  <header class="mb-6 grid grid-cols-[10rem_minmax(0,1fr)] gap-4 sm:grid-cols-[12rem_minmax(0,1fr)]">
    <div>
      <div class="aspect-square overflow-hidden rounded-lg bg-black">
        {#if detail.crop_path || detail.thumbnail_path}
          <img
            src={thumbnailUrl((detail.crop_path ?? detail.thumbnail_path) as string)}
            alt={detail.class_name}
            class="h-full w-full object-cover"
          />
        {:else}
          <div class="grid h-full place-items-center text-s text-baba-text-faint">
            {t("identities_no_thumb")}
          </div>
        {/if}
      </div>
      {#if detail.label?.cover_photo_path}
        <div class="mt-1 grid"><Button size="small" onclick={() => setCoverPhoto(null)} disabled={coverBusy}>{coverBusy ? t("identities_cover_busy") : t("identities_cover_clear")}</Button></div>
      {/if}
    </div>
    <div class="flex min-w-0 flex-col justify-between">
      <div>
        <div class="flex flex-wrap items-center gap-2">
          <!-- Detector class vs operator-tagged kind: hide the detector
               chip when the operator-set kind explicitly covers the
               detected class (kind=pet + class=cat is the canonical
               Lumi case — one chip is enough, two is noise). Also
               drops the identical-spelling case naturally. -->
          {#if detail.label?.species}
            <!-- Identity's own species beats the detector's flip. -->
            <Tag tone="busy">
              {detail.label.species}
            </Tag>
          {:else if detail.class_name && !classChipRedundant(detail.label?.kind, detail.class_name)}
            <Tag tone="busy">
              {classLabel(detail.class_name)}
            </Tag>
          {/if}
          {#if detail.label?.kind}
            <Tag tone="busy">
              {kindLabel(detail.label.kind)}
            </Tag>
          {/if}
          {#if detail.label?.plate}
            <span class="font-mono"><Tag>
              {detail.label.plate}
            </Tag></span>
          {/if}
          {#if detail.label?.source === "ai"}
            <Tag tone="busy" title={t("identities_ai_draft_title")}>✨ {t("identities_ai_draft")}</Tag>
          {/if}
          {#if detail.label?.has_reference_embedding}
            <Tag tone="busy" title={t("identities_enrolled_title")}>★ {t("identities_enrolled")}</Tag>
          {/if}
          {#if detail.class_id === 0}
            {#if detail.face_confirmed}
              <Tag tone="ok" title={t("identities_face_confirmed_hint")}>{t("identities_face_confirmed_badge")}</Tag>
            {:else}
              <Tag tone="busy" title={t("identities_appearance_only_hint")}>{t("identities_appearance_only_badge")}</Tag>
            {/if}
          {/if}
          <span class="text-s text-baba-text-faint font-mono">{detail.global_id.slice(0, 8)}</span>
        </div>
        <h2 class="mt-2 text-2xl font-semibold">
          {#if detail.label?.name}
            {detail.label.name}
            <span class="ml-2 text-l font-normal text-baba-text-faint">
              · {detail.n_tracks} {t("identities_tracks_label")}
            </span>
          {:else}
            {detail.n_tracks} {t("identities_tracks_label")}
          {/if}
        </h2>
        {#if detail.label?.tags && detail.label.tags.length > 0}
          <div class="mt-2 flex flex-wrap gap-1">
            {#each detail.label.tags as tag (tag)}
              <Tag tone="quiet">#{tag}</Tag>
            {/each}
          </div>
        {/if}
      </div>
      <div>
        <dl class="grid grid-cols-1 gap-y-1 text-m sm:grid-cols-3 sm:gap-x-6">
          <div>
            <dt class="text-s text-baba-text-faint">{t("identities_first_seen")}</dt>
            <dd class="text-baba-text">{dt.full(detail.first_seen)}</dd>
          </div>
          <div>
            <dt class="text-s text-baba-text-faint">{t("identities_last_seen")}</dt>
            <dd class="text-baba-text">{dt.full(detail.last_seen)}</dd>
          </div>
          <div>
            <dt class="text-s text-baba-text-faint">{t("identities_cameras_label")}</dt>
            <dd class="text-baba-text font-mono">{detail.cameras.join(" · ")}</dd>
          </div>
        </dl>
        <div class="mt-3 flex flex-wrap gap-2">
          <Button size="small" onclick={openLabelEdit}>{t("identities_label_edit")}</Button>
          <Button size="small" onclick={openMergePicker}>{t("identities_merge_button")}</Button>
          {#if !detail.label}
            <!-- Anonymous cluster: the user is HERE, looking at the photos —
                 assigning them to an existing identity must be one click, not
                 "go find that identity's page and pull me in from there". -->
            <Button tone="accent" size="small" onclick={openAssignPicker}>{t("identities_assign_button")}</Button>
          {/if}
          <div class="ml-auto"><Button tone="danger" size="small" onclick={onDeleteIdentity} disabled={deleteBusy}>{deleteBusy ? t("identities_delete_busy") : t("identities_delete_button")}</Button></div>
        </div>
      </div>
    </div>
  </header>

  {#if assignOpen}
    <div class="mb-6"><Card title={t("identities_assign_title")}>
      {#snippet actions()}
        <Button size="small" onclick={() => (assignOpen = false)}>{t("identities_merge_cancel")}</Button>
      {/snippet}
      <p class="mb-3 text-s text-baba-text-faint">{t("identities_assign_hint")}</p>
      {#if assignLoading}
        <p class="text-m text-baba-text-faint">{t("identities_merge_loading")}</p>
      {:else if assignCandidates.length === 0}
        <p class="text-m text-baba-text-faint">{t("identities_assign_empty")}</p>
      {:else}
        <ul class="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-6">
          {#each assignCandidates as c (c.global_id)}
            {@const likely = c.nearest_dist !== null && c.nearest_dist < IDENTITY_AUTO_MATCH_THRESHOLD}
            <li>
              <button
                type="button"
                onclick={() => doAssign(c)}
                disabled={assignBusy}
                class="relative block w-full overflow-hidden rounded border text-left disabled:opacity-50
                  {likely
                    ? 'border-baba-accent/60 bg-baba-accent/5 ring-1 ring-baba-accent/40 hover:bg-baba-accent/10'
                    : 'border-baba-border bg-baba-panel-2 hover:bg-baba-panel'}"
              >
                <div class="aspect-square bg-black">
                  {#if c.crop_path || c.thumbnail_path}
                    <img
                      src={thumbnailUrl((c.crop_path ?? c.thumbnail_path) as string)}
                      alt={c.label?.name ?? c.class_name}
                      class="h-full w-full object-cover"
                      loading="lazy"
                    />
                  {/if}
                </div>
                <div class="px-2 py-1.5">
                  <div class="truncate text-s font-medium">{c.label?.name}</div>
                  {#if c.nearest_dist !== null}
                    <div class="text-2xs {likely ? 'text-baba-accent' : 'text-baba-text-faint'}">
                      {likely ? t("identities_assign_likely") + " · " : ""}{formatNumber(c.nearest_dist, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                    </div>
                  {/if}
                </div>
              </button>
            </li>
          {/each}
        </ul>
      {/if}
    </Card></div>
  {/if}

  {#if mergeOpen}
    <div class="mb-6"><Card title={t("identities_merge_title")}>
      {#snippet actions()}
        <div class="flex items-center gap-3">
          {#if mergeSelected.size > 0}
            <Button tone="primary" size="small" onclick={doMergeSelected} disabled={mergeBusy !== null}>{t("identities_merge_selected_into")} {detail?.label?.name ?? t("identities_unnamed")} ({mergeSelected.size})</Button>
          {/if}
          <Button size="small" onclick={() => (mergeOpen = false)}>{t("identities_merge_cancel")}</Button>
        </div>
      {/snippet}
      <p class="mb-3 text-s text-baba-text-faint">{t("identities_merge_hint")}</p>
      {#if mergeLoading}
        <p class="text-m text-baba-text-faint">{t("identities_merge_loading")}</p>
      {:else if mergeCandidates.length === 0}
        <p class="text-m text-baba-text-faint">{t("identities_merge_empty")}</p>
      {:else}
        <ul class="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-6">
          {#each mergeCandidates as c (c.global_id)}
            {@const likely = c.nearest_dist !== null && c.nearest_dist < IDENTITY_AUTO_MATCH_THRESHOLD}
            {@const selected = mergeSelected.has(c.global_id)}
            {@const named = !!c.label?.name}
            <li>
              <button
                type="button"
                onclick={() => toggleMergeSelect(c.global_id)}
                disabled={mergeBusy !== null}
                class="relative block w-full overflow-hidden rounded border text-left disabled:opacity-50
                  {selected
                    ? 'border-baba-accent bg-baba-accent/15 ring-2 ring-baba-accent'
                    : likely
                      ? 'border-baba-accent/60 bg-baba-accent/5 ring-1 ring-baba-accent/40 hover:bg-baba-accent/10'
                      : 'border-baba-border bg-baba-panel-2 hover:bg-baba-panel'}"
              >
                {#if selected}
                  <span class="absolute right-1 top-1 z-10 grid h-5 w-5 place-items-center rounded-full bg-baba-accent text-xs font-bold text-baba-on-accent">✓</span>
                {/if}
                <div class="aspect-square bg-black">
                  {#if (c.crop_path || c.thumbnail_path) && !mergeImgFailed.has(c.global_id)}
                    <img
                      src={thumbnailUrl((c.crop_path ?? c.thumbnail_path) as string)}
                      alt={c.class_name}
                      class="h-full w-full object-cover"
                      loading="lazy"
                      onerror={() => { const n = new Set(mergeImgFailed); n.add(c.global_id); mergeImgFailed = n; }}
                    />
                  {:else}
                    <div class="grid h-full place-items-center text-2xs text-baba-text-faint">
                      {t("identities_no_thumb")}
                    </div>
                  {/if}
                </div>
                <div class="space-y-0.5 p-2 text-xs">
                  <div class="flex items-center justify-between">
                    <span class="truncate {named ? 'font-medium text-baba-text' : 'font-mono text-baba-text-muted'}">{c.label?.name ?? c.global_id.slice(0, 8)}</span>
                    {#if c.nearest_dist !== null}
                      <span class="rounded px-1 text-2xs font-medium
                        {likely ? 'bg-baba-accent/30 text-baba-accent' : 'bg-baba-accent/10 text-baba-accent'}">
                        {formatNumber(c.nearest_dist, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                      </span>
                    {/if}
                  </div>
                  <div class="text-baba-text-faint">
                    {c.n_tracks}× · {dt.short(c.last_seen)}
                  </div>
                  {#if likely}
                    <div class="font-medium text-baba-accent">★ {t("identities_match_likely")}</div>
                  {/if}
                  {#if mergeBusy === c.global_id}
                    <div class="text-baba-accent">{t("identities_merge_busy")}</div>
                  {/if}
                </div>
              </button>
            </li>
          {/each}
        </ul>
      {/if}
    </Card></div>
  {/if}

  {#if labelEditOpen}
    <div class="mb-6"><Card title={t("identities_label_section")}>
      {#snippet actions()}
        <Button tone="accent" size="small" onclick={askAi} disabled={aiBusy}>{aiBusy ? t("identities_ai_describing") : `✨ ${t("identities_ai_describe")}`}</Button>
      {/snippet}
      {#if aiHint}
        <p class="mb-2 text-s text-baba-accent">{aiHint}</p>
      {/if}
      {#if aiError}
        <p class="mb-2 text-s text-red-400">
          {t("identities_ai_failed").replace("{msg}", aiError)}
          {#if aiRaw}
            <span class="ml-2 text-baba-text-faint">{t("identities_ai_unparseable").replace("{raw}", aiRaw.slice(0, 200))}</span>
          {/if}
        </p>
      {/if}
      <div class="grid grid-cols-1 gap-3 md:grid-cols-2">
        <label class="block">
          <span class="block text-s text-baba-text-muted">{t("identities_label_name")}</span>
          <input
            type="text"
            bind:value={labelDraft.name}
            class="mt-1 w-full rounded border border-baba-border bg-baba-panel-2 px-2 py-1.5 text-m focus:border-baba-accent focus:outline-none"
            placeholder={t("identities_label_name_placeholder")}
          />
        </label>
        <label class="block">
          <span class="block text-s text-baba-text-muted">{t("identities_label_kind")}</span>
          <select
            bind:value={labelDraft.kind}
            class="mt-1 w-full rounded border border-baba-border bg-baba-panel-2 px-2 py-1.5 text-m focus:border-baba-accent focus:outline-none"
          >
            {#each KIND_OPTIONS as opt (opt.key)}
              <option value={opt.key}>{t(opt.label_key)}</option>
            {/each}
            {#if labelDraft.kind && !STANDARD_KINDS.has(labelDraft.kind as KindKey)}
              <!-- Preserve a pre-existing non-standard kind so opening + saving
                   the form doesn't clobber whatever the operator (or AI) typed.
                   Switching to any standard option drops this. -->
              <option value={labelDraft.kind}>
                {t("identities_label_kind_custom").replace("{value}", labelDraft.kind)}
              </option>
            {/if}
          </select>
        </label>
        {#if labelDraft.kind === "vehicle"}
          <label class="block">
            <span class="block text-s text-baba-text-muted">{t("identities_label_plate")}</span>
            <input
              type="text"
              bind:value={labelDraft.plate}
              class="mt-1 w-full rounded border border-baba-border bg-baba-panel-2 px-2 py-1.5 text-m font-mono focus:border-baba-accent focus:outline-none"
              placeholder={t("identities_label_plate_placeholder")}
            />
          </label>
          <label class="block">
            <span class="block text-s text-baba-text-muted">{t("identities_label_linked_person")}</span>
            <select
              bind:value={labelDraft.linked_person}
              onfocus={loadPersonOptions}
              class="mt-1 w-full rounded border border-baba-border bg-baba-panel-2 px-2 py-1.5 text-m focus:border-baba-accent focus:outline-none"
            >
              <option value={null}>{t("identities_label_linked_none")}</option>
              {#each personOptions as p (p.gid)}
                <option value={p.gid}>{p.name}</option>
              {/each}
            </select>
            <span class="mt-1 block text-xs text-baba-text-faint">{t("identities_label_linked_hint")}</span>
          </label>
        {/if}
        <div class="md:col-span-2">
          <span class="block text-s text-baba-text-muted">{t("identities_label_affiliation")}</span>
          <div class="mt-1 flex flex-wrap gap-1.5">
            <Picks picks={AFFILIATION_CHIPS.map((a) => ({ key: a.key, label: t(a.label_key) }))} chosen={[labelDraftAffiliation]} onpick={(k) => (labelDraftAffiliation = k)} />
          </div>
        </div>
        {#if labelDraft.kind === "person" || labelDraft.kind === "pet"}
          <label class="flex cursor-pointer items-center gap-2 text-s text-baba-text-muted md:col-span-2">
            <input type="checkbox" bind:checked={labelDraftResident} class="accent-baba-accent" />
            {t("identities_label_resident")}
          </label>
        {/if}
        {#if labelDraft.kind === "pet"}
          <label class="block">
            <span class="block text-s text-baba-text-muted">{t("identities_label_species")}</span>
            <input
              bind:value={labelDraftSpecies}
              placeholder="cat / dog"
              class="mt-1 w-full rounded border border-baba-border bg-baba-panel-2 px-2 py-1.5 text-m focus:border-baba-accent focus:outline-none"
            />
          </label>
        {/if}
        <label class="block md:col-span-2">
          <span class="block text-s text-baba-text-muted">{t("identities_label_tags")}</span>
          <input
            type="text"
            bind:value={labelDraftTagsText}
            class="mt-1 w-full rounded border border-baba-border bg-baba-panel-2 px-2 py-1.5 text-m focus:border-baba-accent focus:outline-none"
            placeholder={t("identities_label_tags_placeholder")}
          />
        </label>
        <label class="block md:col-span-2">
          <span class="block text-s text-baba-text-muted">{t("identities_label_notes")}</span>
          <textarea
            bind:value={labelDraft.notes}
            rows="3"
            class="mt-1 w-full rounded border border-baba-border bg-baba-panel-2 px-2 py-1.5 text-m focus:border-baba-accent focus:outline-none"
            placeholder={t("identities_label_notes_placeholder")}
          ></textarea>
        </label>
      </div>
      {#if labelError}
        <p class="mt-3 text-m text-red-400">{labelError}</p>
      {/if}
      <div class="mt-4 flex items-center gap-3">
        <SaveButton dirty={labelDirty} saving={labelBusy} onclick={saveLabel} />
        <Button size="small" onclick={() => (labelEditOpen = false)}>{t("identities_label_cancel")}</Button>
        {#if detail.label}
          <div class="ml-auto"><Button tone="danger" size="small" onclick={deleteLabel}>{t("identities_label_delete")}</Button></div>
        {/if}
      </div>
    </Card></div>
  {/if}

  <div class="mb-6"><Card title={t("identities_refs_section")}>
    {#snippet actions()}
      {#if detail?.label && detail.label.reference_count > 0}
        <span class="text-s text-baba-text-muted">
          {t("identities_refs_count").replace("{n}", String(detail.label.reference_count))}
          {#if detail.label.has_reference_embedding}<span class="ml-1 text-baba-accent">★</span>{/if}
        </span>
      {/if}
    {/snippet}
    <p class="mb-3 text-s text-baba-text-faint">{t("identities_refs_help")}</p>
    <div class="flex flex-wrap items-center gap-2">
      <label class="inline-block">
        <input
          type="file"
          accept="image/*"
          multiple
          onchange={onPhotoFiles}
          disabled={refsUploading || refsAutoBusy}
          class="hidden"
        />
        <span
          class="inline-block cursor-pointer rounded bg-baba-accent px-3 py-1.5 text-m font-medium text-baba-on-accent hover:opacity-90
                 {refsUploading || refsAutoBusy ? 'opacity-50' : ''}"
        >{refsUploading ? t("identities_refs_uploading") : t("identities_refs_upload")}</span>
      </label>
      <Button tone="accent" onclick={autoPickReferences} disabled={refsAutoBusy || refsUploading || !detail.n_tracks} title={t("identities_refs_auto_pick_hint")}>{refsAutoBusy
          ? t("identities_refs_auto_pick_busy")
          : t("identities_refs_auto_pick").replace("{n}", String(AUTO_PICK_COUNT))}</Button>
      <Button tone="accent" onclick={enrolFaceSamples} disabled={refsFaceBusy || refsUploading || refsAutoBusy || !detail.n_tracks} title={t("identities_refs_face_samples_hint")}>{refsFaceBusy
          ? t("identities_refs_face_samples_busy")
          : t("identities_refs_face_samples").replace("{n}", String(FACE_SAMPLES_COUNT))}</Button>
      {#if detail.label?.kind === "person" && sources?.immich}
        <Button onclick={openImmichPicker} disabled={immichBusy || opusBusy || refsUploading || refsAutoBusy} title={t("identities_refs_immich_hint")}>{immichBusy ? t("identities_refs_immich_busy") : t("identities_refs_immich")}</Button>
      {/if}
      {#if detail.label?.kind === "person" && sources?.opus}
        <Button onclick={openOpus} disabled={opusBusy || immichBusy || refsUploading || refsAutoBusy} title={t("identities_refs_opus_hint")}>{opusBusy
            ? t("identities_refs_opus_busy")
            : detail.label?.opus_person_id
              ? t("identities_refs_opus_more")
              : t("identities_refs_opus")}</Button>
      {/if}
      {#if refsFacelessCount > 0}
        <div class="ml-auto"><Button tone="danger" onclick={purgeFacelessReferences} disabled={refsPurgeBusy || refsUploading || refsAutoBusy || opusBusy || immichBusy} title={t("identities_refs_purge_faceless_hint")}>{refsPurgeBusy
            ? t("identities_refs_purge_faceless_busy")
            : t("identities_refs_purge_faceless").replace("{n}", String(refsFacelessCount))}</Button></div>
      {/if}
    </div>

    {#if immichOpen}
      <div class="mt-3 rounded border border-baba-border bg-baba-panel-2 p-3">
        {#if immichLoading}
          <p class="text-m text-baba-text-muted">{t("identities_refs_immich_loading")}</p>
        {:else if !immichConfigured}
          <p class="text-m text-amber-400">
            {t("identities_refs_immich_unconfigured")}
            <a href="/settings/face-recognition" class="underline hover:text-amber-300">
              {t("identities_refs_immich_settings_link")}
            </a>
          </p>
        {:else}
          <div class="mb-2 flex items-center gap-2">
            <input
              type="search"
              bind:value={immichFilter}
              placeholder={t("identities_refs_immich_search")}
              class="w-full rounded border border-baba-border bg-baba-panel px-2 py-1.5 text-m"
            />
            <div class="shrink-0"><Button onclick={() => (immichOpen = false)}>{t("common.cancel")}</Button></div>
          </div>
          {#if immichMatches.length === 0}
            <p class="text-m text-baba-text-faint">{t("identities_refs_immich_none")}</p>
          {:else}
            <ul class="max-h-64 divide-y divide-baba-border overflow-y-auto">
              {#each immichMatches as p (p.id)}
                <li>
                  <button
                    type="button"
                    onclick={() => importFromImmich(p)}
                    disabled={immichBusy}
                    class="w-full px-2 py-2 text-left text-m hover:bg-baba-panel disabled:opacity-50"
                  >{p.name}</button>
                </li>
              {/each}
            </ul>
          {/if}
        {/if}
      </div>
    {/if}
    {#if opusOpen}
      <div class="mt-3">
        <OpusPeoplePicker
          seed={detail.label?.name ?? ""}
          busy={opusBusy}
          onpick={(p) => importFromOpus(p)}
          oncancel={() => (opusOpen = false)}
        />
      </div>
    {/if}
    {#if refsLastResult}
      <p class="mt-3 text-s text-baba-text-muted">
        {t("identities_refs_used").replace("{n}", String(refsLastResult.used))}
        {#if refsLastResult.skipped.length > 0}
          · <span class={refsLastResult.used === 0 ? "text-amber-400" : "text-red-400"}>
              {t("identities_refs_skipped").replace("{list}", refsLastResult.skipped.join("; "))}
            </span>
        {/if}
      </p>
    {/if}
    {#if refsError}
      <p class="mt-3 text-m text-red-400">{refsError}</p>
    {/if}

    {#if refsListLoading}
      <p class="mt-3 text-s text-baba-text-faint">{t("identities_refs_loading")}</p>
    {:else if refsList.length === 0}
      <p class="mt-3 text-s text-baba-text-faint">{t("identities_refs_none")}</p>
    {:else}
      {#snippet refCard(p: IdentityReferencePhoto, zoomFace: boolean)}
          {@const isRefCover = !!p.photo_path && detail?.label?.cover_photo_path === p.photo_path}
          <li class="relative overflow-hidden rounded border bg-baba-panel-2
                     {isRefCover ? 'border-baba-accent/60 ring-1 ring-baba-accent/40' : 'border-baba-border'}">
            <div class="relative aspect-square overflow-hidden bg-black">
              {#if p.photo_path}
                <img
                  src={api.referencePhotoUrl(p.photo_path)}
                  alt="reference"
                  class="h-full w-full object-cover"
                  loading="lazy"
                  use:faceZoom={zoomFace ? p.face_bbox : null}
                />
              {:else}
                <div class="grid h-full place-items-center px-2 text-center text-2xs text-baba-text-faint">
                  {t("identities_refs_no_original")}
                </div>
              {/if}
            </div>
            <!-- Wraps rather than clips: the badge plus both buttons do not fit
                 one line at the narrowest grid column, and justify-between was
                 pushing Delete off the card edge. -->
            <div class="flex flex-wrap items-center justify-between gap-1 p-1.5 text-2xs">
              {#if faceBadgeRelevant(detail?.label?.kind)}
                <span class={p.has_face ? "text-baba-accent" : "text-baba-text-faint"}>
                  {p.has_face ? t("identities_refs_face_detected") : t("identities_refs_no_face_detected")}
                  {#if zoomFace && p.face_px}<span class="text-baba-text-faint">{Math.round(p.face_px)}px</span>{/if}
                  <span class="text-baba-text-faint" title={t("identities_refs_source_hint")}>· {refSource(p.source)}</span>
                </span>
              {:else}
                <span></span>
              {/if}
              <div class="flex items-center gap-1">
                {#if p.photo_path}
                  <Button size="small" selected={isRefCover} onclick={() => setCoverPhoto(isRefCover ? null : p.photo_path)} disabled={coverBusy}>{isRefCover ? t("identities_cover_current") : t("identities_cover_set")}</Button>
                {/if}
                <Button tone="danger" size="small" onclick={() => deleteReferencePhoto(p)} disabled={refsDeleting === p.id}>× {t("identities_refs_delete")}</Button>
              </div>
            </div>
          </li>
      {/snippet}

      <!-- Face and body are different claims about the same person, so they are
           listed separately: a face reference is what recognition actually runs
           on, a body-only reference contributes nothing to it. Keeping them in
           one grid hid that a set could look well-stocked while carrying almost
           no face evidence. -->
      {#if refsFace.length}
        <p class="mt-4 text-s font-medium uppercase tracking-wide text-baba-text-muted">
          {t("identities_refs_group_face").replace("{n}", String(refsFace.length))}
        </p>
        <p class="mt-1 text-xs text-baba-text-faint">
          {t("identities_refs_group_face_hint")}
        </p>
        <ul class="mt-2 grid grid-cols-3 gap-3 sm:grid-cols-4 md:grid-cols-6 lg:grid-cols-8">
          {#each refsFace as p (p.id)}{@render refCard(p, true)}{/each}
        </ul>
      {/if}
      {#if refsBody.length}
        <p class="mt-5 text-s font-medium uppercase tracking-wide text-baba-text-muted">
          {t("identities_refs_group_body").replace("{n}", String(refsBody.length))}
        </p>
        <p class="mt-1 text-xs text-baba-text-faint">
          {detail.label?.kind === "person"
            ? t("identities_refs_group_body_hint_person")
            : t("identities_refs_group_body_hint")}
        </p>
        <ul class="mt-2 grid grid-cols-3 gap-3 sm:grid-cols-4 md:grid-cols-6 lg:grid-cols-8">
          {#each refsBody as p (p.id)}{@render refCard(p, false)}{/each}
        </ul>
      {/if}
    {/if}
  </Card></div>

  {#if presence.length > 0}
    <!-- Stays, not tracks: the presence registry's episodes. A seated person
         fragments into dozens of tracks below; up here that is ONE row with a
         start, an end and a duration — the record that answers "how long was
         he on the patio". -->
    <section class="mb-6">
      <h3 class="mb-3 text-m font-semibold uppercase tracking-wide text-baba-text-muted">
        {t("identities_presence_label")}
      </h3>
      <div class="overflow-hidden rounded-lg border border-baba-border bg-baba-panel">
        <ul class="divide-y divide-baba-border">
          {#each presence as ep (ep.camera_id + ep.present_since)}
            <li class="flex items-center gap-3 px-4 py-2 text-m">
              <span class="w-24 shrink-0 truncate text-baba-text">{ep.camera_name}</span>
              <span class="tabular-nums text-baba-text-muted">
                {dt.short(ep.present_since)}{ep.departed_at ? ` – ${dt.hm(ep.departed_at)}` : ""}
              </span>
              <span class="tabular-nums text-baba-text-faint">
                {formatUptime(ep.duration_s)}
              </span>
              {#if !ep.departed_at}
                <Tag tone="ok">
                  {t("identities_presence_now")}
                </Tag>
              {/if}
              <span class="ml-auto"><Tag tone={ep.evidence === "face" ? "busy" : "quiet"}>
                {ep.evidence === "face" ? t("identities_evidence_face") : t("identities_evidence_body")}
              </Tag></span>
            </li>
          {/each}
        </ul>
      </div>
    </section>
  {/if}

  <section>
    <div class="mb-3 flex flex-wrap items-center justify-between gap-2">
      <h3 class="text-m font-semibold uppercase tracking-wide text-baba-text-muted">
        {t("identities_tracks_label")}
      </h3>
      {#if detail.n_tracks > 1}
        <div class="flex items-center gap-2 text-s">
          {#if selectedTracks.size > 0}
            <Button size="small" onclick={selectNone}>{t("identities_select_none")}</Button>
          {:else}
            <Button size="small" onclick={selectAll}>{t("identities_select_all")}</Button>
          {/if}
          <Button tone="accent" size="small" onclick={promoteSelectedTracks} disabled={selectedTracks.size === 0 || refsPromoteBusy || bulkSplitProgress !== null}>
            {#if refsPromoteBusy}
              {t("identities_refs_promote_busy")}
            {:else}
              {t("identities_refs_promote_selected").replace("{n}", String(selectedTracks.size))}
            {/if}
          </Button>
          <Button tone="primary" size="small" onclick={doBulkSplit} disabled={selectedTracks.size === 0 || bulkSplitProgress !== null || refsPromoteBusy}>
            {#if bulkSplitProgress}
              {t("identities_split_busy")
                .replace("{done}", String(bulkSplitProgress.done))
                .replace("{total}", String(bulkSplitProgress.total))}
            {:else}
              {t("identities_split_selected").replace("{n}", String(selectedTracks.size))}
            {/if}
          </Button>
          <Button tone="danger" size="small" onclick={deleteSelectedSightings} disabled={selectedTracks.size === 0 || deleteSightingsBusy || bulkSplitProgress !== null || refsPromoteBusy}>
            {deleteSightingsBusy
              ? t("identities_delete_sightings_busy")
              : t("identities_delete_sightings_selected").replace("{n}", String(selectedTracks.size))}
          </Button>
        </div>
      {/if}
    </div>
    <ul class="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
      {#each detail.tracks as track (track.id)}
        {@const isCover = !!track.crop_path && detail.label?.cover_photo_path === track.crop_path}
        <li class="overflow-hidden rounded-lg border bg-baba-panel transition-colors
                   {selectedTracks.has(track.id)
                     ? 'border-baba-accent/60 ring-1 ring-baba-accent/40'
                     : 'border-baba-border'}">
          <div class="relative aspect-video bg-black">
            {#if track.crop_path || track.thumbnail_path}
              <img
                src={thumbnailUrl((track.crop_path ?? track.thumbnail_path) as string)}
                alt={track.class_name}
                class="h-full w-full object-{track.crop_path ? 'contain' : 'cover'}"
                loading="lazy"
              />
            {:else}
              <div class="grid h-full place-items-center text-s text-baba-text-faint">
                {t("identities_no_thumb")}
              </div>
            {/if}
            {#if detail.n_tracks > 1}
              <label class="absolute left-2 top-2 cursor-pointer rounded bg-black/60 p-1">
                <input
                  type="checkbox"
                  checked={selectedTracks.has(track.id)}
                  onchange={() => toggleTrack(track.id)}
                  class="h-4 w-4 accent-baba-accent"
                />
              </label>
            {/if}
          </div>
          <div class="space-y-1 p-3 text-m">
            <div class="flex items-center justify-between">
              <span class="font-medium">{track.camera.name}</span>
              <span class="text-s text-baba-text-faint">
                {track.duration_s !== null ? `${formatNumber(track.duration_s, { minimumFractionDigits: 1, maximumFractionDigits: 1 })}s` : ""}
              </span>
            </div>
            <div class="text-s text-baba-text-faint">{dt.full(track.started_at)}</div>
            <div class="flex flex-wrap items-center justify-between gap-2">
              <div class="text-s text-baba-text-muted">
                {track.n_observations} obs · {displayClass(track.class_name)}{#if track.class_id === 0} · <span
                  class={track.has_face ? "text-emerald-400" : "text-baba-text-faint"}
                  title={track.has_face ? t("identities_face_confirmed_hint") : t("identities_appearance_only_hint")}
                >{track.has_face ? t("identities_sighting_face") : t("identities_sighting_no_face")}</span>{/if}
              </div>
              <div class="flex items-center gap-1">
                {#if track.crop_path}
                  <Button size="small" selected={isCover} onclick={() => setCoverPhoto(isCover ? null : track.crop_path)} disabled={coverBusy}>{isCover ? t("identities_cover_current") : t("identities_cover_set")}</Button>
                {/if}
                {#if detail.n_tracks > 1}
                  <Button size="small" onclick={() => doSplit(track.id)} disabled={splitBusy !== null}>{t("identities_split_button")}</Button>
                {/if}
                <Button tone="danger" size="small" onclick={() => deleteOneSighting(track.id)} disabled={deleteSightingsBusy}>{t("dialog_delete")}</Button>
              </div>
            </div>
          </div>
        </li>
      {/each}
    </ul>
  </section>

{/if}
