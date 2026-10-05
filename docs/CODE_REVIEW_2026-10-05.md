# BABA code review

This review examines BABA's source, service contracts, data lifecycle, security boundaries, frontend behaviour and deployment configuration. The architecture has a clear division between camera I/O, shared inference and persistent metadata. The most consequential remaining defects concern the video proxy's authorization, state being discarded before successful persistence, and notification filters depending on track rows that do not yet exist.

The baseline contains **21 findings: 5 P1, 14 P2 and 2 P3**. Every finding below preserves the original trigger, source evidence, correction and acceptance check. Following the user's instruction to continue with fixes, all 21 have been addressed in the working tree. The implementation record below distinguishes source corrections from production rollout.

## Correction status — 5 October 2026

All entries below are **implemented and locally verified; production deployment is pending**. Historical evidence and line references in the findings describe the review baseline, before these corrections.

| Finding | Implemented correction | Verification |
| --- | --- | --- |
| 01 | One canonical proxy implementation shared by Vite and the production server rejects traversal, repeated encoding and authority changes before forwarding | Actual HTTP proxy regression and rebuilt production server |
| 02 | Viewer/operator media requests accept one registered camera `src` only; management paths and producer parameters require admin | HTTP proxy regression, real API authentication and go2rtc smoke |
| 03 | Expired, parked and retired-generation tracks transfer to an owned pending queue; failed transactions retry with a stable UUID and idempotent events | Injected failures, retry coordinator and PostgreSQL transaction rollback/replay |
| 04 | Scene runtime advances only after the transition transaction commits | Same classification retries after an injected commit failure |
| 05 | Zone-event payloads carry the class independently of the future track row; notification matching reads it when the join is absent | Live-event SQL regression before track finalization |
| 06 | Revisioned face settings activate after API and embedder acknowledge the loaded pair; embedder reload serializes with inference and native readers; recompute waits for activation | Real SQL concurrent/stale acknowledgements, locked producer reload, model-space and enrolment regressions, UI activation scheduling |
| 07 | Detector, body/face embedder and native face inference have a finite deadline that exits the process for restart when native work stalls | Real child-process watchdog expiry and native exception discrimination |
| 08 | Failed listener registration/reconciliation closes the connection before every retry, startup failure and cancellation | Repeated failed reconciliations with connection ownership assertions |
| 09 | API face listener reconciles on reconnect; event manager reloads application tunables and active face settings, propagating read failures for reconnect retry | Listener recovery and SQL-backed tunable/active-model reconciliation regressions |
| 10 | Recovery-code consumption succeeds only for the request that atomically claims the unused row | Two SQL-backed concurrent logins forced past verification together |
| 11 | Track and plate-read retention own row locks through file removal and metadata deletion; locked rows are skipped | Concurrent SQL deadline extension preserves the track and its media |
| 12 | SAM2 initialization and complete image/prediction operations share a lock | Two overlapping requests keep their own input images |
| 13 | Rule filters validate UUIDs, bounded integer class IDs and known keys; corrupt stored rules report their own error and do not stop subsequent rules | Invalid-input cases and dispatcher isolation regression |
| 14 | Camera mutations capture their target and discard responses after navigation; shared busy state serializes edits | Deferred-response frontend regressions |
| 15 | Zone-map writes serialize; controls lock during save and apply the returned zone before the next edit | Frontend overlapping-edit regressions |
| 16 | Live and activity setup check disposal before creating feeds and ignore stale callbacks | Cleanup-before-setup and navigation regressions |
| 17 | Zone class pickers use the camera's effective detection rules and guard stale responses | Per-camera override and navigation regressions |
| 18 | Clip timestamps must be finite and representable; query bounds are capped before work, encoding narrows the rows, and codec choice is recomputed for the final window | Thirty-day request, nonfinite timestamps and mixed-codec regressions |
| 19 | Timed-out or cancelled codec probes kill and reap their subprocess | Timeout and cancellation regressions |
| 20 | Clearing/replacing visual search invalidates in-flight generations | Deferred visual-search response regressions |
| 21 | Architecture now describes face-based naming, bounded body grouping, OSNet body embeddings and DINOv2 scene embeddings | Documentation checked against current producer and matcher code |

Executed correction checks: **37 targeted Python tests**, **20 frontend concurrency tests**, the Node HTTP proxy regression, Ruff, Svelte static checks (zero errors and warnings), shell syntax and diff whitespace. The Python tests use PostgreSQL 18 with all **107 migrations** and isolated data. No full application suite was run from the CLI.

The CPU base, API and production web images were rebuilt under temporary review tags. `baba-fix-api` and `baba-fix-web` were started against disposable PostgreSQL, NATS and go2rtc containers on an isolated network namespace without host ports. Smoke checks covered the page, health, login, visible face-activation failure for deliberately unconfigured models, traversal rejection, viewer restrictions and admin access. These containers were removed after verification. The frontend build emits adapter-generated empty-environment-chunk and bundler timing diagnostics; the static application checks remain clean.

No commit, push or production deployment was performed. Rollout requires the new base images and consuming service images, API migration 107 before the dependent services start, and a production web rebuild. The existing Compose API health dependencies provide the migration start gate. Real GPU model loading, camera feeds and production service health remain target-host acceptance checks; the isolated smoke environment intentionally contains no camera or learned-model data.

## Review baseline

| Item | Value |
| --- | --- |
| Review date | 5 October 2026, Europe/Zagreb, CEST |
| Repository | `/mnt/docker/baba` |
| Commit | `a626103271728f173160e0be7597556c6b795a0f` |
| Shared backend submodule | `core/src/home_core` at `8fffcb6b48963487e8e3b9a4756ec63123c5527c` |
| Shared UI submodule | `web/src/lib/kit` at `94f99245c6f367ea40ae32ba16131bf8c36de2e9` |
| Initial working tree | Clean |
| Deliverable | Source review and implementation plan |

P1 means a substantial authorization failure, loss of persisted history or failure of core alarm behaviour. P2 means a functional, concurrency or recovery defect that needs correction after the P1 group. P3 means a smaller user experience or documentation defect. These are engineering priorities for this application, not CVSS scores.

**Reproduced** means the actual source function or handler was exercised with controlled inputs and scheduling. Test dependencies were substituted where stated; this does not imply an end-to-end production test. **Source traced** means the failure follows from the inspected producer and consumer paths, but was not exercised against running camera or GPU services.

## Coverage

| Area | Review focus |
| --- | --- |
| Core and shared libraries | Wire types, backend selection, model metadata, storage paths, shared-memory lifecycle, notification reconnection and health markers |
| Inference and decoder backends | ONNXRuntime, TensorRT, OpenVINO, software, Intel and NVIDIA decoder contracts; selected hardware and failure behaviour |
| Ingestor and detector | Frame ownership, batching, preprocessing, rule resolution, inference execution and liveness |
| Tracker and embedder | Track lifecycle, asynchronous embedding, face model selection, native face readers and model tags |
| Event manager | Finalization, zone events, identity matching, scene consumption, configuration reconciliation and persistence boundaries |
| State evaluator | Hysteresis, scene transitions, transactions, place updates and progress monitoring |
| Recorder and media API | Segment indexing, codec probes, retention, media paths, clip construction and subprocess cleanup |
| API and database | Authentication, recovery codes, roles, identities, reference photos, notification rules, camera and zone settings, AI helpers and migrations |
| Frontend | Production proxy, API calls, camera navigation, live subscriptions, zone editing, face settings, activity and visual search |
| Supporting services and deployment | Doorbell and hardware telemetry lifecycle, Docker configuration, shell scripts, deployment and recovery gates |

The review was risk directed across the repository. Deeper control-flow and fault analysis concentrated on authorization, persistence, alarms, settings propagation and asynchronous ownership. Supporting services and hardware-specific paths received source and configuration review; they were not run against their target devices.

Existing project memory and earlier audits were used to distinguish current defects from repaired issues and accepted policies. In particular, local storage of camera credentials and ephemeral NATS/notification delivery were treated as established decisions. The external disclosure in finding 01 is a separate authorization defect.

## Verification performed

All Python execution and Node tooling ran in disposable containers. Source mounts were read only. Database probes used a disposable PostgreSQL 18 instance with pgvector, no published ports and fabricated records. The go2rtc probe used version 1.9.14 with fabricated streams and networking restricted to its container. Production was not modified or restarted.

| Check | Result and scope |
| --- | --- |
| Python syntax | 267 Python files parsed successfully, including shared source present in the checkout |
| Dependency lock | `uv lock --check --offline` succeeded |
| Python static lint | Ruff succeeded with no findings |
| Frontend static check | `npm run check` reported 0 errors and 0 warnings in a disposable writable copy |
| Shell syntax | 22 deployment, script and tooling shell files passed `bash -n` |
| Compose validation | CPU, Intel and NVIDIA configurations parsed with explicit fabricated credentials and storage paths |
| Fresh schema | All 106 migrations applied successfully to disposable PostgreSQL 18 with pgvector |
| Targeted probes | Authorization, SQL races, asynchronous lifecycle, failure recovery and resource bounds reproduced the defects identified below |

Static checks passing does not establish correctness of failure paths or concurrent requests. The probes below deliberately exercise those paths. The full pytest/vitest suite, production rebuild, hardware inference benchmark, image vulnerability scan and live camera end-to-end test were not performed.

## Findings

### P1 01 Viewer access can disclose go2rtc configuration

**Evidence:** [web/server.mjs](../web/server.mjs#L64), authorization at lines 64–70 and forwarding at lines 143–156. go2rtc 1.9.14 is pinned in [docker-compose.yml](../docker-compose.yml#L252).

Authorization checks the raw path against a permissive media prefix. Forwarding then assigns that path to a `URL`, which normalizes dot segments. An authenticated viewer or peer can request `/go2rtc/api/stream.mp4/../config` or `/go2rtc/api/frame.jpeg/%2e%2e/config`. Both pass the media check and reach `/api/config` with the proxy's go2rtc administrator credentials. The upstream configuration handler returns configuration contents, which include configured camera source URLs. [go2rtc configuration handler](https://raw.githubusercontent.com/AlexxIT/go2rtc/v1.9.14/internal/api/config.go)

**Verification:** Reproduced with the actual production proxy source, a fabricated viewer authentication response and a mock upstream. Direct `/go2rtc/api/config` returned 403; both traversal requests reached `/api/config` with administrator authentication. No real credentials were read. This finding establishes read access; it does not establish write, restart or code execution access.

**Correction:** Canonicalize once before both authorization and forwarding. Reject traversal, including encoded dot segments, and authorize exact supported consumer routes.

**Acceptance:** Viewer and peer requests using literal or encoded traversal fail before upstream access. Legitimate video and snapshot routes continue to work; direct administrative access remains restricted.

### P1 02 Video query parameters can replace streams and fetch internal URLs

**Evidence:** [web/server.mjs](../web/server.mjs#L156) forwards the entire query string through the same authenticated media surface.

Path authorization alone does not make a media GET read only. go2rtc's MP4 handler calls `GetOrPatch`; when `src` is a URL rather than an existing stream name, this can create or change a stream. The `name` parameter can select an existing stream to replace. An allowed request such as `/go2rtc/api/stream.mp4?src=http://127.0.0.1:1984/review-missing&name=review_camera` therefore changes stream state and causes an HTTP fetch. Unrestricted HTTP sources also expose a server-side request path into networks reachable from go2rtc. [MP4 handler](https://raw.githubusercontent.com/AlexxIT/go2rtc/v1.9.14/internal/mp4/mp4.go), [stream resolution](https://raw.githubusercontent.com/AlexxIT/go2rtc/v1.9.14/internal/streams/streams.go), [HTTP source handler](https://raw.githubusercontent.com/AlexxIT/go2rtc/v1.9.14/internal/http/http.go)

**Verification:** Reproduced against disposable go2rtc 1.9.14. A fabricated stream's producer changed from its original RTSP URL to the supplied loopback HTTP URL even though video creation returned HTTP 500. The BABA proxy source forwards those parameters unchanged. Execution sources are restricted by upstream validation; this report does not claim command execution.

**Correction:** For non-administrative consumers, resolve `src` to an existing allowed camera stream. Reject URL producers, `name` and other producer-changing parameters. Apply an explicit query contract to every permitted transport, including WebSocket setup.

**Acceptance:** A viewer or peer cannot create or replace streams, or cause requests to arbitrary URL sources. Authorized playback using registered stream names still succeeds.

### P1 03 Failed track finalization permanently loses its retry state

**Evidence:** [event manager sweep](../services/event-manager/src/baba_event_manager/__main__.py#L1730), especially lines 1746–1760.

The sweep removes an expired track from `_state` before `_finalize` succeeds. Parked tracks are marked finalized before their write succeeds. A transient database or finalization exception is logged, but the track is not restored or queued for retry. Subsequent sweeps cannot persist it. The suppressed-track insertion path also absorbs write errors, so retry success must be explicit across finalization paths.

**Verification:** Reproduced using the actual `_sweep_loop`, a fabricated expired track and an injected finalization failure. After two sweep opportunities, there was one finalization attempt and zero retained track states.

**Correction:** Keep an owned pending-finalization record until persistence commits. Retry with the preallocated UUID and idempotent writes; set finalized flags only after success. Handle shutdown through the same pending lifecycle.

**Acceptance:** Inject one database failure during normal and parked finalization. Recovery writes each track and its event once, without losing media references or duplicating a sighting.

### P1 04 A failed scene transition advances memory without advancing the database

**Evidence:** [state evaluator transition decision](../services/state-evaluator/src/baba_state_evaluator/__main__.py#L712), and [transition transaction](../services/state-evaluator/src/baba_state_evaluator/__main__.py#L917).

The evaluator assigns `rt.current_state = raw_label` before awaiting the transaction that updates scene status, inserts an event and changes place state. If that transaction fails, runtime state remains ahead of persistent state. The next identical classification fails the `raw_label != rt.current_state` condition, so the transition is not retried. A gate or occupied place can remain externally incorrect until another change or reconciliation.

**Verification:** Reproduced through the actual `_tick_region` with deterministic classifications and an injected commit failure. Two identical fresh classifications produced one commit attempt; runtime state was `open` while the fabricated persistent state remained `closed`.

**Correction:** Advance committed runtime state after the database transaction succeeds. Preserve a retryable transition while maintaining hysteresis and idempotent event/place writes.

**Acceptance:** One failed commit followed by the same classification eventually produces the intended state, one transition event and the matching place update.

### P1 05 Class filtered zone alarms depend on a track that has not been finalized

**Evidence:** [zone event producer](../services/event-manager/src/baba_event_manager/__main__.py#L618), [notification event query](../services/api/src/baba_api/rules_dispatcher.py#L116), [class filter](../services/api/src/baba_api/rules_dispatcher.py#L78), and worker at lines 403–428.

Zone events occur during a track's lifetime and carry its future database UUID. Track rows are inserted at finalization. The dispatcher obtains class information only by joining `tracks`, so a normal live `zone_enter` or `zone_dwell` has no class at dispatch time and fails a `class_ids` filter. It is processed once. Object parked/unparked events emitted without a database track context cannot obtain a class from that join at all. Unfiltered notification titles also lose the available class name.

**Verification:** Reproduced with actual migrated PostgreSQL and the real event-query/filter functions. A person zone event failed `{class_ids: [0]}` before finalization and passed after a person track row was inserted. The worker does not revisit the earlier event.

**Correction:** Persist canonical class information with each applicable event and dispatch from that event contract. Define class filtering for event kinds without tracks, rather than relying on eventual finalization.

**Acceptance:** A person-specific zone alarm fires immediately while its track is still active. Relevant parked-object alarms work without a finalized track; other classes remain excluded.

### P2 06 Face model selection activates incompatible producer and matcher states

**Evidence:** [face settings update](../services/api/src/baba_api/routes_face_recognition.py#L235), [API stack reload](../services/api/src/baba_api/__main__.py#L173), [matcher settings refresh](../services/event-manager/src/baba_event_manager/__main__.py#L561), [embedder listener](../services/embedder/src/baba_embedder/__main__.py#L310), and [face UI save](../web/src/lib/FaceRecognitionCard.svelte#L110).

Saving model B updates the database and notifies the API and event manager. The UI immediately starts reference recomputation. The embedder reads face selection only at startup and listens only for camera changes, so it continues producing model A embeddings. Matching correctly requires the selected model tag for both samples and references, as shown in [live identity matching](../services/event-manager/src/baba_event_manager/live_identity.py#L308). That correctness guard then rejects the producer's incompatible samples.

**Verification:** Source traced across settings, startup, notification and matching paths. No live GPU model switch was performed.

**Correction:** Make activation a coordinated configuration revision: reload the embedder and its native face reader, confirm the loaded revision, and align reference recomputation and matching with that active revision. Surface pending or failed activation in the UI.

**Acceptance:** Switch between two installed models while services run. All producers, references and matchers converge on one acknowledged model; load failure is visible and cannot present a partially applied selection as successful.

### P2 07 Native inference can stall while health checks continue to pass

**Evidence:** [detector inference](../services/detector/src/baba_detector/__main__.py#L488), [detector heartbeat](../services/detector/src/baba_detector/__main__.py#L672), [embedder inference](../services/embedder/src/baba_embedder/__main__.py#L423), face inference at line 518 and heartbeat at line 750; [Compose detector check](../docker-compose.yml#L361); [healwatch](../scripts/heal-unhealthy.sh#L60).

Inference runs in worker threads without an operation deadline. A native driver or model call can block that thread while the asyncio loop and its independent heartbeat continue scheduling. The service remains healthy to Docker and healwatch, although shared detection or embedding work has stopped. The state evaluator already has a separate progress watchdog; this finding concerns the detector and embedder paths above.

**Verification:** Reproduced the liveness mechanism with a blocked native-call substitute and the actual shared `HealthMarker`. Inference remained in flight while marker modification times advanced. No hardware hang was induced.

**Correction:** Track active operation deadlines separately from idle input. On a confirmed native stall, terminate the affected process loudly so it can restart. Cancelling an asyncio await alone does not stop a blocked native thread.

**Acceptance:** No cameras feeding frames leaves the service healthy and idle. An intentionally blocked in-flight inference triggers a bounded visible failure and process recovery.

### P2 08 Reconnection leaks successful connections when reconciliation fails

**Evidence:** [ResilientListener](../core/src/baba_core/pg_listen.py#L101), especially `_reconnect` at lines 117–134.

Reconnection closes the previous connection once, before its retry loop. A new connection can succeed and then its `on_connect` reconciliation can fail. The next attempt overwrites `_conn` without closing the newly opened connection from the failed attempt. Repeated failures retain connections and their listeners, consuming PostgreSQL connection slots. Startup initialization has the same need to close on partial failure.

**Verification:** Reproduced through the actual reconnect loop with connection doubles and two injected reconciliation failures. Three connections opened and none closed before success; stopping the listener closed only the last one.

**Correction:** Close each failed attempt before backoff, including listener-registration and reconciliation failures. Assign ownership deliberately and cover cancellation during initialization.

**Acceptance:** Reconciliation can fail repeatedly without increasing live database connections. Successful recovery and shutdown leave exactly the intended listener connection, then none.

### P2 09 Settings missed during a LISTEN outage are not fully reconciled

**Evidence:** [API face listener](../services/api/src/baba_api/__main__.py#L173), [event manager reconciliation](../services/event-manager/src/baba_event_manager/__main__.py#L425), and [settings notifications](../services/event-manager/src/baba_event_manager/__main__.py#L501).

The API face listener has no `on_connect` refresh. The event manager reconciles cameras, zones, scenes and lighting, but not application tunables or face settings. If the dedicated LISTEN connection is unavailable while the ordinary pool remains usable, settings can commit successfully and their notifications are missed. Reconnection restores subscriptions without loading those settings. The API also has a startup gap between its first face settings read and listener registration.

**Verification:** Source traced through the shared listener's reconnect contract and each consumer's refresh callbacks.

**Correction:** Include all retained configuration in every initial and reconnect reconciliation. Establish listening before the authoritative refresh so a settings change cannot fall into the startup gap.

**Acceptance:** Disconnect only LISTEN, change face settings and tunables through the API, then reconnect. Running services adopt database values without another save or restart.

### P2 10 Recovery codes can be consumed successfully more than once

**Evidence:** [recovery code consumption](../services/api/src/baba_api/routes_auth.py#L318).

Two concurrent logins can both select the same unused recovery code and verify it. Each then performs an unconditional update by ID and returns success. The update neither checks `used_at IS NULL` nor verifies that this caller won consumption, so the single-use authentication property is not enforced atomically.

**Verification:** Reproduced with actual PostgreSQL and `_consume_recovery_code`; a verification barrier substituted for password hashing synchronized the requests. Both returned `true` for one recovery-code row.

**Correction:** Consume with a conditional update and `RETURNING`, returning success only to the request that transitions an unused code to used. Keep verification and consumption semantics explicit.

**Acceptance:** Two simultaneous submissions of the same valid code yield exactly one successful consumption. Later reuse fails.

### P2 11 Retention can delete a track whose deadline was just extended

**Evidence:** [track sweeper](../services/api/src/baba_api/tracks_retention_sweeper.py#L81), [enrollment retention extension](../services/api/src/baba_api/routes_identities/_reference_photos.py#L519), and [labeling extension](../services/api/src/baba_api/routes_identities/_core.py#L838).

The sweeper selects expired tracks, unlinks media in a thread and then deletes by ID without rechecking eligibility or owning the row against concurrent extensions. Enrollment or labeling can extend a selected deadline while unlinking is pending. The sweep subsequently deletes the protected track and its crops. Rechecking only the final DELETE would still leave the protected media missing.

**Verification:** Reproduced through `_prune_one_batch` with actual PostgreSQL and a temporary fabricated crop. After selection, the deadline was extended by 90 days. The sweep still deleted the row and crop.

**Correction:** Make deletion eligibility an owned transition coordinated with retention extensions. Resolve the race before destructive file operations; preserve retryability if filesystem removal fails.

**Acceptance:** Enrollment racing a sweep has a defined winner. A successfully extended, surviving track retains its media. Failed unlinks leave cleanup state that can be retried safely.

### P2 12 Concurrent SAM2 requests share mutable image state

**Evidence:** [cached predictor](../services/api/src/baba_api/routes_ai.py#L346), [polygon refinement](../services/api/src/baba_api/routes_ai.py#L385), [point segmentation](../services/api/src/baba_api/routes_ai.py#L645), threaded invocation at line 709 and [SAM2 image cache](../core/src/baba_core/sam2.py#L125).

One predictor is cached on application state. Each worker thread calls `set_image` and then predicts using shared image embeddings and geometry. Another request can replace those fields between the two operations, so camera A's click can be interpreted against camera B's image. Initial construction is also entered from worker threads without coordinated initialization.

**Verification:** Reproduced with the actual point-segmentation wrapper and a controlled predictor double preserving the same mutable-image contract. Concurrent requests for images 1 and 2 both observed image 2 during prediction.

**Correction:** Own predictor initialization and the complete encode-plus-predict operation through one inference queue or lock, or give each request isolated image state while sharing immutable model resources.

**Acceptance:** Concurrent requests with different images and aspect ratios return results from their own image. First-use concurrency constructs one intended model instance.

### P2 13 Invalid notification filter values can stop dispatch of other rules

**Evidence:** [rule request models](../services/api/src/baba_api/routes_rules.py#L53), [filter validation](../services/api/src/baba_api/routes_rules.py#L92), [runtime coercion](../services/api/src/baba_api/rules_dispatcher.py#L78), and rule iteration at lines 158–165.

The validator checks keys but accepts arbitrary values. `{class_ids: ["person"]}` passes both the model and validator, then raises `ValueError` during dispatch. A scalar `camera_ids` can similarly fail iteration. There is no per-rule error boundary around matching, so one malformed rule can abort processing before other rules are evaluated; the outer worker logs and discards that event.

**Verification:** The actual request model and validator accepted the malformed class filter; the real matcher raised `ValueError` against a valid event.

**Correction:** Validate typed UUID lists and supported numeric class IDs at the API boundary. Reject malformed PATCH values consistently and isolate invalid stored rules so they cannot suppress valid rules.

**Acceptance:** Malformed filters receive a clear 4xx response. If a corrupted rule already exists, other matching rules still dispatch and the bad rule records a visible error.

### P2 14 A late camera save can redirect the next save to another camera

**Evidence:** [camera settings](../web/src/routes/settings/cameras/[id]/+page.svelte#L63), load guard at lines 63–86, save assignment at line 121; similar assignments at lines 105, 213 and 240.

Loads use a sequence guard, but mutations assign returned camera state unconditionally. Save camera A, navigate to B, then let A's delayed response arrive. The route and editing draft remain B while `cam` becomes A. The next save targets `cam.id`, sending B's fields to A.

**Verification:** Reproduced using the page's actual transpiled load and save function bodies with delayed API responses. The second PATCH targeted A with camera B's name.

**Correction:** Capture the target camera and route generation for each mutation. Apply results only if they still belong to the current page; derive write targets from the current route's authoritative identity.

**Acceptance:** Navigate between cameras during save, rename and other mutations. Late responses cannot change the current camera or cause a subsequent write to the old camera.

### P2 15 Rapid zone rule edits overwrite a previously saved field

**Evidence:** [ZoneRulesCard](../web/src/lib/ZoneRulesCard.svelte#L61), editable controls at lines 143–158 and 200–209, and [whole rules replacement](../services/api/src/baba_api/routes_zones.py#L168).

Each edit constructs a complete rules object from the last returned zone. Inputs remain active during a pending save. If cooldown changes from 10 to 20, then confidence changes before the first response, the second payload still contains cooldown 10. This loses the first change even when responses complete in request order.

**Verification:** Actual card function bodies produced consecutive payloads `{confidence: 0.5, cooldown: 20}` and `{confidence: 0.7, cooldown: 10}` under controlled scheduling.

**Correction:** Serialize commits around an authoritative local draft, or disable all relevant controls while a complete replacement is in flight. Coordinate replacement semantics with the API to avoid silent lost updates.

**Acceptance:** Rapid changes to two fields preserve both intended values after all responses settle. A rejected save leaves a visible, recoverable draft.

### P2 16 Live subscriptions can open after their page has been destroyed

**Evidence:** [live camera setup](../web/src/routes/live/[slug]/+page.svelte#L239), cleanup at lines 260–265, and [activity subscriptions](../web/src/routes/activity/+page.svelte#L265).

The live camera effect waits for zones, but its cancelled check controls only assignment. It then opens detection and track feeds even if cleanup already ran. Those feeds missed their cleanup opportunity and their callbacks can update state belonging to a later camera. Activity has the same pattern: an asynchronous mount waits for cameras and can establish its events feed after destruction.

**Verification:** Reproduced the actual live effect with a delayed zone response. Cleanup ran first; both subsequent camera A feeds remained open.

**Correction:** Check disposal immediately before establishing each feed and guard callbacks by route generation. Abort pending setup and give every subscription an owner that can close it even during asynchronous initialization.

**Acceptance:** Leave the page during any pending setup request. No later feed opens or updates the new page; repeated navigation does not accumulate subscriptions.

### P2 17 Zone class selection ignores camera overrides

**Evidence:** [zone class picker](../web/src/lib/ZoneRulesCard.svelte#L40), [canonical rule resolution](../core/src/baba_core/rule_resolve.py#L66), and [zone class restrictions](../services/event-manager/src/baba_event_manager/zones.py#L63).

The picker includes only globally enabled classes. Canonical resolution permits an existing global class disabled globally to be enabled by a camera override. Such a class is detected on that camera but unavailable when configuring a restrictive zone. For example, a camera-specific dog class cannot be added to the zone's allowed classes and its zone events are suppressed.

**Verification:** Source traced across the picker, effective rule resolver and zone allowlist. The camera detection card already exposes camera overrides.

**Correction:** Populate choices from effective rules for the zone's camera, using the same resolution contract as detection. Refresh choices when relevant rules change.

**Acceptance:** A globally disabled class enabled on one camera can be selected in that camera's zones. The picker matches actual effective detection rules for each camera.

### P2 18 Clip limits are applied after unbounded metadata and file work

**Evidence:** [camera clip endpoint](../services/api/src/baba_api/routes_recordings.py#L651), query at lines 674–689, clamping at lines 693–696 and file work at lines 698–705.

The endpoint fetches all segments in the supplied interval before applying its 20-minute copy or 4-minute encoding limit. It then resolves files from that entire result. A request for a month therefore performs month-sized database and filesystem work to return a short clip. Codec selection can also be influenced by segments outside the eventual clip. Non-finite timestamps pass the initial duration comparison and raise unhandled conversion exceptions.

**Verification:** The actual endpoint sent a 720-hour query for a 30-day request despite a maximum copy output of 20 minutes. `inf` and `nan` produced `OverflowError` and `ValueError` rather than controlled request errors.

**Correction:** Validate finite, ordered, representable timestamps before conversion. Bound the first query by the absolute maximum clip window, then narrow rows to the final codec-specific window before filesystem and concat work.

**Acceptance:** Database rows and filesystem visits are bounded by the maximum supported clip duration. Invalid times return 4xx, and codec changes outside the final window cannot alter the selected clip policy.

### P2 19 Timed out codec probes are not terminated or reaped

**Evidence:** [recorder codec probe](../services/recorder/src/baba_recorder/worker.py#L148), especially subprocess creation and timeout handling at lines 168–177.

The recorder times out `proc.communicate()` after ten seconds, logs and returns without terminating or waiting for the ffprobe child. A blocked child can remain alive; the codec cache remains empty and later indexing can launch another probe. Cancellation needs the same child ownership cleanup.

**Verification:** Reproduced with the actual `_probe_codec` and a timed-out process double. The method returned `None` without kill or reap calls.

**Correction:** Terminate and reap the child on timeout and cancellation, with a bounded terminate-to-kill escalation. Keep a diagnostic result that makes repeated probe failures visible.

**Acceptance:** A deliberately blocked ffprobe leaves no child after timeout, service shutdown or task cancellation. Subsequent probes cannot accumulate orphan processes.

### P3 20 Clearing visual search does not invalidate its pending response

**Evidence:** [visual search](../web/src/routes/search/+page.svelte#L74), response guard at lines 93–102 and clear action at lines 106–113.

Search responses are guarded by `searchSeq`, but clearing the search does not advance that sequence or abort the request. A late successful response restores results after the image and previous results were cleared, leaving results with no visible query image.

**Verification:** Source traced through the existing request-generation guard and clear action.

**Correction:** Invalidate the active generation and reset loading when clearing or disposing the search. Abort the request where the client supports cancellation.

**Acceptance:** Clear while search is pending; its later success or failure cannot restore results or errors.

### P3 21 The system map still describes body embeddings as identity authority

**Evidence:** [ARCHITECTURE.md](../ARCHITECTURE.md#L19), current [live identity matching](../services/event-manager/src/baba_event_manager/live_identity.py#L308), and the project's face-only identity decisions.

The principles state that body embeddings identify people across cameras and days and that a face is never required. The current naming design uses face evidence for names; body embeddings have bounded anonymous grouping and face-anchor continuation roles. The introductory system map therefore teaches a different security and identity contract from the implementation and project decisions.

**Verification:** Source and project-decision comparison. This is documentation drift, not a request to change the face-only identity design.

**Correction:** Update the canonical system map to describe current identity authority, model-space restrictions and the limited role of body embeddings. Have other explanatory documents point to that contract.

**Acceptance:** Architecture, user help and model settings explain the same conditions for anonymous grouping and naming a person.

## Implementation order

1. **Close authorization paths:** findings 01 and 02 together. Authorization must cover the canonical path and the query contract; fixing only traversal leaves producer-changing GET requests available.
2. **Preserve authoritative history and alarms:** findings 03, 04, 05 and 11. Establish commit-before-forget semantics, retry ownership, event class data and retention ownership. Verify with injected failures and concurrent enrollment.
3. **Make recovery and configuration deterministic:** findings 06–10, 12, 13, 18 and 19. Coordinate model activation, make health reflect in-flight work, close failed connection/process attempts, reconcile all settings, enforce single-use recovery and constrain expensive media work.
4. **Correct asynchronous editing and navigation:** findings 14–17 and 20, then align documentation in 21. Cover late responses, disposal and multi-field edits with controlled scheduling.

This was the implementation order for the original baseline. The full set has now been addressed; the correction-status section records verification and the remaining production rollout boundary.

## Architecture and maintainability assessment

The per-camera I/O and shared-inference topology is appropriate for the intended workload. The storage layout, hardware variant selection and model tags provide useful central contracts. Fresh migrations, lock consistency and static checks give a sound baseline for making the corrections.

The recurring weakness is ownership across asynchronous boundaries: a track is forgotten before a write, a scene advances before commit, a settings value is declared selected before all producers adopt it, and a page assumes cleanup has covered resources created later. Correcting these state transitions is more valuable than broad cosmetic refactoring.

The largest service entrypoints contain orchestration, mutable state and persistence together. Further extraction should follow the corrected boundaries: pending finalizations, model activation, event contracts and inference operation ownership. Preserve one authoritative implementation of each contract and avoid compatibility layers or duplicate stores.

The shared-memory writer already invalidates a slot before overwriting pixels and publishes completed metadata afterward. The review did not establish a new torn-frame defect there. Similarly, model-tag filtering is a valid protection against cross-model face comparisons; finding 06 concerns activation coordination around that protection.

## Limits of the conclusion

The findings describe the reviewed checkout and reproducible failure conditions. They do not establish how often any defect has occurred in production. Live configuration can override file defaults, so this document does not assert the active production model, threshold, camera settings or hardware performance.

Hardware-specific inference, decoder drivers, camera reconnect behaviour under real network loss, learned-model quality, recording throughput and disk failure behaviour require target-host checks. Fresh migration success does not prove every upgrade path from an existing populated database. Dependency and image vulnerability scanning and a complete weights-license assessment were outside the executed checks.

The review is complete as a repository-wide source assessment with targeted verification. It is not an end-to-end production acceptance result. The report is the canonical record for these findings; project memory should link here rather than maintain a second copy of the review.
