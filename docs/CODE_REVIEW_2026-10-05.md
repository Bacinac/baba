# BABA code review

This review examines BABA's source, service contracts, data lifecycle, security boundaries, frontend behaviour and deployment configuration. The architecture has a clear division between camera I/O, shared inference and persistent metadata. The most consequential baseline defects concerned the video proxy's authorization, state being discarded before successful persistence, and notification filters depending on track rows that did not yet exist.

The baseline contains **21 findings: 5 P1, 14 P2 and 2 P3**. Every finding below preserves the original trigger, source evidence, correction and acceptance check. All 21 were corrected and deployed in `92acd3b`, version 1.0.22. The follow-up section records additional corrections requested after that rollout.

## Correction status — 5 October 2026

All 21 entries below are **implemented, verified and deployed** on production and both reference instances. Historical evidence and line references in the findings describe the review baseline, before these corrections.

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

Initial correction checks: **37 targeted Python tests**, **20 frontend concurrency tests**, the Node HTTP proxy regression, Ruff, Svelte static checks (zero errors and warnings), shell syntax and diff whitespace. The Python tests use PostgreSQL 18 with all **107 migrations** and isolated data. The configured pre-push hook subsequently passed its required test, dependency, public-content and secret gates.

The CPU base, API and production web images were rebuilt under temporary review tags. `baba-fix-api` and `baba-fix-web` were started against disposable PostgreSQL, NATS and go2rtc containers on an isolated network namespace without host ports. Smoke checks covered the page, health, login, visible face-activation failure for deliberately unconfigured models, traversal rejection, viewer restrictions and admin access. These containers were removed after verification. The frontend build emits adapter-generated empty-environment-chunk and bundler timing diagnostics; the static application checks remain clean.

Commit `92acd3b` was pushed and deployed through `deploy/deploy.sh all`. Both Intel instances had all 14 running services healthy; the NVIDIA reference passed its health gate and was parked by its configured policy. Migration 107 and matching API/embedder face acknowledgements were verified on all three instances, including the production SCRFD/TopoFR pair at its existing threshold 0.75. All seven production cameras had fresh recordings at the final 12:02 CEST check. Public proxy checks returned 400 for traversal and 401 for anonymous administrative access. The public demo was refreshed. These checks establish startup, configuration activation and recording continuity, not recognition quality or a live model-switch benchmark.

## Follow-up corrections — 5 October 2026

The user requested continued changes after the completed rollout, then authorized commit, push and deploy of these four corrections. They were deployed in `eafbcdf`, version 1.0.23. The verification below records the follow-up bundle before that second rollout. The running revision is reported by `/version` and `deploy/deploy.sh --status`.

| Finding | Defect and correction | Verification |
| --- | --- | --- |
| F01 | Cancelling `run_inference` cancelled its deadline while the native thread remained blocked. The wrapper now retains ownership and the original deadline until the thread finishes; repeated cancellation cannot bypass process recovery. | A real child process reproduced the old hang; normal and cancelled blocked operations now exit within the watchdog bound. A released native thread completes before cancellation propagates. |
| F02 | Auto-recompute intent existed only in the face settings card, so navigation lost it. Migration 108 adds the detector and settings revision to durable pending jobs. Settings saves queue the intent in the same transaction; one API worker claims it only after both activation acknowledgements. Restart resumes current work, newer selections supersede pending work, and stale native results cannot overwrite references. The card attaches to the stored job on reload. | PostgreSQL regressions cover activation failure/retry, supersession, concurrent claims, restart, lost completion writes, manual duplicate requests and stale native results. Frontend regressions cover reload, pending/running transitions, rebasing and late responses. |
| F03 | A departing vehicle reused its arrival record while parked finalization was retrying. Observation now treats a queued parked closure as a closed visit, preserving the pending arrival snapshot and allocating a separate departure UUID. | Injected finalization failure followed by departure and recovery preserves two distinct visits; committing the arrival cannot mark the departure finalized. |
| F04 | Successful polling did not clear old activation/job network errors. Separate polling errors now clear on recovery, preserve Save validation errors and keep retrying the final settings refresh. | Deferred-response frontend regressions cover error recovery, disposal and superseding saves. |

Follow-up checks: **37 targeted Python tests**, **27 frontend concurrency tests**, Ruff, Svelte static checks (zero errors and warnings), i18n consistency and whitespace checks. Fresh PostgreSQL 18 applied all **108 migrations**. CPU base, API, event manager and production web images were rebuilt under temporary follow-up tags. Actual API/web runtime checks verified login, settings, pending job creation and preservation of the same job after API restart; the event manager connected to PostgreSQL and NATS and maintained its health marker. These checks use fabricated credentials, no real cameras or model weights, and no host ports. This is the local verification snapshot before the authorized follow-up commit, push and deploy.

## Service lifecycles and event contracts — 5 October 2026

After the version 1.0.23 rollout, the user requested the next steps and then authorized publication of this bundle. This implements the pending-finalization, API face-activation and track-event boundaries recommended by the architecture assessment. Four additional findings cover shutdown ownership, motion-event correlation and stable event classification. The verification below records the locally checked implementation before its rollout. The running revision is reported by `/version` and `deploy/deploy.sh --status`.

### P2 F05 Shutdown finalizes tracks before the last subscription messages are processed

**Evidence:** [event-manager shutdown](../services/event-manager/src/baba_event_manager/__main__.py) finalized and cleared active records before draining NATS. The drain can still invoke the track handler, recreating active records after the finalization pass. Those records disappear when the process exits.

**Verification:** A regression runs the actual service lifecycle against migrated PostgreSQL and delivers five observations through its subscription handler during the controlled NATS drain. The version 1.0.23 entrypoint writes zero tracks; the corrected entrypoint writes one track and one event with all five observations.

**Correction:** Stop background loops, the settings listener and statistics publisher, then drain NATS before detaching active tracks for finalization. Finalization writes its events through PostgreSQL and does not require an open NATS connection. The existing ten-second finalization deadline now also bounds a stalled attempt. The event-manager container has a thirty-second stop grace period to accommodate the NATS drain, finalization deadline and resource cleanup.

**Ownership:** [FinalizationQueue](../services/event-manager/src/baba_event_manager/finalization.py) is the sole owner of pending snapshots, stable UUIDs, serialized retries, pending parked-visit closure and commit-dependent source cleanup. Observation, generation retirement, sweeps and shutdown use that queue directly; the previous queue fields and forwarding methods are removed. Its pending-count gauge updates on enqueue and successful completion.

**Checks:** Thirteen targeted Python tests passed, including frozen observation snapshots, failed commits, cancellation, concurrent drains, enqueue during a drain, separate parked arrival/departure records, transaction retry and bounded shutdown. Ruff and Compose validation passed. A rebuilt CPU event-manager image ran against disposable PostgreSQL 18, real NATS and a synthetic camera snapshot endpoint. Restart persisted three tracks and three finalization events exactly once, each with five observations; the rebuilt container returned healthy and then exited cleanly with no warnings or errors. All disposable containers and their network were removed. No production cameras, GPU inference or production configuration were changed.

### P2 F06 API face reload tasks outlive their listener and database pool

**Evidence:** Each face-settings notification spawned a separate reload task in the API entrypoint. The shutdown path stopped the settings listener without awaiting those reload tasks. A task already inside native model loading could continue after the listener stopped and attempt to acknowledge activation after the database pool closed.

**Verification:** The version 1.0.23 entrypoint was exercised against migrated PostgreSQL with a controlled native loader. Stopping its listener left the reload task alive; releasing the native load after pool closure changed the in-process model and raised `InterfaceError: pool is closed` during acknowledgement.

**Correction:** [ApiFaceActivation](../services/api/src/baba_api/face_activation.py) owns the settings listener, reload lock, pending signal and one reload worker. Repeated notifications coalesce, changes received during loading remain pending, and a stale acknowledgement requests the current selection again. Shutdown rejects new requests, stops the listener and cancels and awaits its worker before database closure. The recompute worker is also cancelled before awaiting activation shutdown, so both native operations retain their original deadlines concurrently. The API container's 150-second stop grace period accommodates the existing 120-second native watchdog and cleanup. Missing detector configuration clears all loaded-model state and retains an explicit activation error; restoring it reloads the selected pair.

**Checks:** Eight PostgreSQL regressions passed: threshold-only revisions reuse models and wait for the embedder acknowledgement; a burst of 100 reload requests coalesces through a superseded load; shutdown retains ownership of a real executor thread and prevents later pool access; empty, mismatched and failing model loads remain visible and recover on retry; missing detector configuration clears stale state and recovers; and a transient settings-read failure retries without another notification. The new test file is included in the API database test gate. Ruff, shell syntax, explicit synthetic Compose validation and whitespace checks passed.

A rebuilt CPU API image ran against disposable PostgreSQL 18, NATS and go2rtc with synthetic credentials and no host ports. Login, authenticated settings reads, a real notification-driven refresh, restart with the selected revision preserved and application shutdown completion were verified without service warnings or errors. Native ownership tests use controlled model factories; the runtime deliberately omits face weights and verifies the resulting explicit configuration diagnostic. These checks do not assess learned-model quality or target GPU behaviour. All disposable containers and networks were removed.

### P2 F07 Motion events lose their visit UUID and a closed arrival hides departure

**Evidence:** The former motion-event producer omitted the optional database UUID when creating `object_parked` and `object_unparked`, so the writer inserted `events.track_id = NULL`. Separately, reopening a closed parked vehicle's visit initialized its motion state to `active` before detecting the transition. The new departure visit therefore lost the parked-to-active edge and emitted no `object_unparked`.

**Verification:** The version 1.0.23 entrypoint failed a real PostgreSQL observation regression: a vehicle moved away after its parked arrival closed, but the event list contained only `object_parked`. The corrected regression covers both an already committed arrival and an arrival still awaiting persistence. Parking links to the arrival UUID; departure links to a fresh UUID; later active observations produce no duplicate departure, and completing the pending arrival does not alter the departure record.

**Correction:** [TrackEvent](../core/src/baba_core/events.py) is the shared immutable transition contract. Every zone or motion event requires a reserved visit UUID and captures its observed class and coordinates. Zone events require their zone UUID. [The event writer](../services/event-manager/src/baba_event_manager/events.py) owns payload serialization and deterministic event IDs based on camera, visit, kind, zone and observation timestamp. The entrypoint resolves camera/zone metadata and increments its event counter only for a new insert. Reopening a departure carries the previous motion state into transition detection while retaining the new visit UUID. The former optional-UUID pending-event structure and writer are removed.

**Checks:** Seven new regression cases passed, including both parked closure states, idempotent persistence, local tracker-ID reuse across separate visits, immutable snapshots and rejected incomplete contracts. All eight cases in the changed observation test file also passed. No schema change or historical event rewrite is required.

### P2 F08 Alarms and the event feed change class when a track finalizes

**Evidence:** The alarm dispatcher preferred the finalized track's majority class over the event payload. An event observed as `car` could therefore match the car rule immediately but the truck rule after finalization, including on a later dispatch attempt. The event feed read class only from the track join: it returned no class while the track was active and later substituted the majority class.

**Verification:** Real PostgreSQL regressions first create a car event without a track row, then finalize the same UUID as a truck. Against the version 1.0.23 API, the car filter stops matching after finalization and the event feed initially reports a null class. Against the corrected API, the car filter and title remain unchanged before and after finalization and after track deletion. The feed preserves the same UUID and observed class while adding the finalized duration.

**Correction:** [Alarm dispatch](../services/api/src/baba_api/rules_dispatcher.py) and [the event feed](../services/api/src/baba_api/routes_events.py) read class exclusively from the event snapshot. The feed still joins finalized tracks and recordings for media, duration and playback metadata. The Sightings view continues to use the finalized track's majority class. The existing API response shape and local tracker ID in the payload are preserved; the database event's track reference is the visit UUID.

**Checks:** Two new PostgreSQL API regressions passed and are included in the API database gate; the seven event-contract cases are included in the event-manager database gate. Together with the changed observation file, **17 targeted Python tests** passed. Ruff, test-runner syntax and whitespace checks passed. Fresh CPU base, API and event-manager images were built with the shared contract included.

Both rebuilt services ran without source overlays against disposable PostgreSQL 18, real NATS, go2rtc and a synthetic JPEG endpoint, with fabricated credentials and no host ports. Seven observations of three person tracks produced a correlated parked/unparked pair. Authenticated HTTP reads returned its UUID and observed class before track persistence. Restarting the event manager persisted all three tracks and their finalization events exactly once; the motion events kept their IDs and class and gained duration and thumbnail metadata. Both services restarted healthy and completed shutdown without warnings or errors. All disposable containers and their network were removed. Separate vehicle arrival/departure UUIDs and majority-class changes are covered by the PostgreSQL regressions; this runtime does not exercise cameras, GPU inference or model quality.

## Remaining lifecycle coverage and documentation — 5 October 2026

The 21 baseline findings and F01–F08 were published in version 1.0.24 (`ecc4e03a`). F09–F16 were subsequently published in version 1.0.25 (`6f606056`): **all 37 source-review corrections are deployed**. The running revision and rollout status are reported by `/version` and `deploy/deploy.sh --status`.

Final continuation checks: **69 targeted Python cases** across the four changed test files, including real PostgreSQL concurrency; **29 frontend concurrency cases** and Svelte zero errors/warnings. Fresh CPU base and all nine changed service images were built. API and event-manager exchanged real NATS observations and authenticated HTTP event responses, restarted, and closed cleanly. Ingestor, recorder and doorbell started and restarted their real supervisors against isolated PostgreSQL/NATS with no enabled cameras. Baked detector-rule, tracker-settings/zone, scene and embedder owners each started and stopped twice; scene/embedding model objects were substituted in those lifecycle checks. No source-package overlays were used. All disposable runtime containers and networks were removed.

The production web image was also rebuilt, served its compiled application through the real Node server, proxied authenticated login/camera requests to the isolated API, and restarted successfully. The demo build, changed-file Ruff, shell syntax, all three Compose variants and public-content gate passed. Real target evidence includes Intel Arc OpenVINO model startup and seven VAAPI decoders, plus an isolated OSNet CUDA run with fallback disabled: its profiler recorded **391 CUDA nodes**, no CPU nodes, finite `16 × 512` output and input dimensions read from the loaded engine. These checks do not constitute a recognition-quality or throughput benchmark. The NVIDIA reference remained parked and no production configuration or model selection was changed.

### P2 F09 Native-operation protection misses scene, plate, tracker and API work

**Evidence:** Scene embeddings, plate inference, the tracker's dedicated OSNet executor and API search/reference/SAM2 paths still used cancellable thread awaits. Cancelling such an await releases its surrounding lock while the native operation continues. The scene watchdog depends on configured evaluable regions and does not protect an interactive capture with none configured.

**Correction:** [Native operations](../core/src/baba_core/native.py) share one deadline and cancellation ownership implementation, with an optional dedicated executor and copied request context. Scene, plate, tracker and API model operations use it, as do media mutations that must finish before releasing resources. The former inference-only implementation is removed. Relevant services have a 150-second shutdown grace period for the 120-second native deadline.

**Verification:** Controlled cancellation retains the real scene lock and dedicated executor until the native thread ends. A child process exercising the actual scene embedding path exits on deadline with no configured regions. Existing cancelled-operation and native-exception regressions remain covered.

### P2 F10 Background work outlives service pools and readers

**Evidence:** Notification handlers and supervisor loops kept global task references but were not awaited by their service before resource closure. API accepted cleanup jobs, SSE teardown and doorbell push inserts had the same lifetime gap. A notification already queued at listener shutdown could admit more work.

**Correction:** [TaskOwner](../core/src/baba_core/task_owner.py) closes admission and joins work owned by each service or component. Configuration owners cancel obsolete reads before pool closure; accepted API cleanup and doorbell inserts finish normally. API provider-usage writes are awaited directly. Listener dispatch rejects queued notifications after stop. Embedder and scene evaluator drain subscriptions before detaching frame readers. An installation without a doorbell reports normal disabled status at INFO; configured camera names still require validation.

**Verification:** Tests block actual bridge callbacks and SSE cleanup, then verify their completion before owner shutdown returns. Native work delays owner shutdown until its thread finishes; late callbacks are closed without being scheduled. Failed updates retain visible logging and completed tasks release their references.

### P2 F11 Cancellation breaks retention's file and metadata boundary

**Evidence:** Cancelling a track or plate retention await could release its transaction's row locks while unlinking continued in another thread. Waiting only for that thread still rolls the transaction back after files have disappeared, leaving durable rows pointing at missing media. Recorder batches had a corresponding gap between file and row deletion.

**Correction:** Track and plate transactions finish as owned batches before cancellation is reported. Recorder retention and disk-prune batches finish their corresponding metadata deletion; an accepted purge owns its complete mutation. Native unlinking remains deadline-protected. The batch owner and service pool lifetime are coordinated rather than shielding only the filesystem call.

**Verification:** Real PostgreSQL tests cancel blocked track and plate deletion, prove that concurrent `FOR UPDATE NOWAIT` cannot claim their rows, release the worker, and confirm that both media and metadata are gone. A recording-batch cancellation regression proves the same file/metadata completion boundary.

### P2 F12 Scene enrolment persists references to failed JPEG writes

**Evidence:** `_write_crop` ignored a false `cv2.imwrite` return and swallowed write exceptions. Capture then inserted a prototype referring to a file that did not exist.

**Correction:** Crop writes propagate failure, run as owned native operations, and precede prototype insertion. Capture returns its existing visible error response when writing fails.

**Verification:** The real capture handler receives a failed OpenCV write; its response reports the failure, no prototype is inserted, and no in-memory prototype is adopted.

### P2 F13 A failed zone-rule save discards the operator's draft

**Evidence:** A rejected save discarded the edited cooldown; retry or another edit could submit the old value. Parent refreshes could also overwrite that draft. The event API's TypeScript duration field additionally omitted the null value used while a track is pending.

**Correction:** The zone's rule draft is retained before submission, survives same-zone refreshes, and has an explicit save retry. Successful save or navigation clears its dirty state. Pending event duration is typed as nullable.

**Verification:** Two deferred-response frontend regressions cover retry and subsequent editing after rejection, including a parent refresh. All 29 cases in the changed concurrency file pass; Svelte reports zero errors and warnings.

### P2 F14 Demo builds use host tools and temporarily mutate application source

**Evidence:** Demo scrubbing/build steps invoked host Python/Pillow and rewrote `app.html` and fixtures in the canonical checkout during a build. Concurrent development or an interrupted build could observe or retain that temporary source.

**Correction:** A container-only helper uses Python 3.14 and Pillow from the existing dependency lock with wheel hashes. Build changes happen in a disposable web tree; privacy gates remain mandatory, resolve policy from the canonical repository context, and produce a separate output artifact.

**Verification:** A complete local demo build passed both privacy gates, wired seven camera stills and clips, and wrote 18 prototype thumbnails. The source template hash and fixture absence were unchanged. No public demo was published during this continuation.

### P3 F15 The roadmap describes missing components that are already implemented

**Evidence:** The July roadmap claimed Intel software decoding, no test framework, no idempotent finalization, no retention sweeper, no backup/restore and no shared formatting. It also mixed historical model proposals with the current runtime and counted 37 migrations.

**Correction:** The local project guide `ROADMAP.md` now links to this canonical correction record, records source-backed component status and separates future product work from review defects. The local project instructions point at the current documents; the published architecture includes RF-DETR and cancellation ownership.

**Verification:** Source paths confirm Intel VAAPI/Quick Sync, container test gates, retry/idempotent writers, both retention implementations, backup/restore, shared UI formatting and 108 migrations. Read-only production logs independently confirm the detector on Intel Arc A380 and VAAPI decode on seven cameras.

### P2 F16 ONNX Runtime can retry failed GPU execution on CPU

**Evidence:** The provider guard checked initialization only. The installed SDK's real `InferenceSession.run` catches execution-provider failure and can rebind to fallback providers before retrying. A successfully initialized CUDA session therefore did not enforce the application's failure policy throughout execution.

**Correction:** The canonical provider guard disables runtime fallback after checking the bound provider, covering both the small-model session factory and ONNX Runtime detector backend.

**Verification:** A regression invokes the installed SDK's actual execution method with an injected provider failure and proves that the exception escapes without rebinding. A separate isolated NVIDIA model run confirms successful CUDA execution with fallback disabled; its input dimensions come from loaded model metadata.

## Operational recovery acceptance — 5 October 2026

The next roadmap step exercises populated-database recovery through the canonical backup and restore scripts. It uncovered F17 below, which is included in this recovery publication: **38 corrections implemented and verified**. The previous 37 corrections were published in 1.0.25; this change adds the transactional restore and its required regression gate.

### P1 F17 Restore modifies the database before detecting an unreadable dump

**Evidence:** [The former restore script](../scripts/restore.sh) extracted the outer archive and checked its declared migration version, then stopped services and ran a nontransactional `pg_restore --clean`. An inner dump truncated halfway through its data still passed `pg_restore --list`. Restore dropped and recreated objects before detecting EOF. In the isolated reproduction, both identity labels and recording rows fell from their populated counts to zero, and the app service remained stopped.

**Correction:** Restore checks that the configured, healthy PostgreSQL container belongs to the selected Compose project. It reads and decompresses the entire dump before stopping services or altering data. App services must stop successfully; PostgreSQL remains running. Every database DROP, COPY and CREATE executes in one transaction. Restore failure leaves app services stopped and reports the transaction outcome as requiring verification. The former branch that proceeded after Compose or service-stop failure is removed. A manifest with a known migration version is required.

**Regression checks:** [The container integration test](../tests/test_backup_restore.sh), included in the required push gate, uses PostgreSQL 18 and a populated relational fixture. It verifies that an unreadable dump leaves table contents, constraints and service uptime unchanged; another Compose project or an unknown schema is rejected; an injected service-stop failure prevents database restore; a target-side dependency error rolls back database changes and leaves the app stopped; and valid recovery restores data, media and the protected secret file. PostgreSQL is not restarted during these operations. The exported-commit gate also exposed an unintended backup abort when the optional runtime revision stamp is absent; backup now records the intended `unknown` revision and still requires a fully readable dump.

**Populated acceptance:** A read-only copy of an existing production checkpoint was restored into an isolated PostgreSQL 18 container, then passed through the canonical backup and corrected restore scripts. Before and after fingerprints match across **38 tables and 1,266,031 rows**, **653 media files**, and **two encrypted provider settings**. The existing API image started and served authenticated HTTP reads for seven cameras, all 13 named identities and both decrypted, masked settings. All **465 durable database references** to reference photos and scene crops resolve to files after restore.

The containers use an internal network with no published ports. Camera connections are disabled only in the disposable database before API startup. API checks use the explicit CPU variant for metadata and authorization, with model loading unconfigured; they do not exercise the restored camera feeds or GPU models. This verifies populated data, media, secrets and API recovery. It does not establish fresh-host model provisioning, hardware throughput, disk-failure handling or archive consistency during simultaneous production writes. Production code, configuration and data were not changed by this exercise.

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

## Baseline verification performed

During the original source review, all Python execution and Node tooling ran in disposable containers. Source mounts were read only. Database probes used a disposable PostgreSQL 18 instance with pgvector, no published ports and fabricated records. The go2rtc probe used version 1.9.14 with fabricated streams and networking restricted to its container. Production was not modified or restarted at that stage; the later authorized rollout is recorded above.

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

Static checks passing does not establish correctness of failure paths or concurrent requests. The probes below deliberately exercise those paths. At the baseline review stage, the full pytest/vitest suite, production rebuild, hardware inference benchmark, image vulnerability scan and live camera end-to-end test had not been performed.

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

This was the implementation order for the original baseline. The full set has now been addressed and deployed; the follow-up sections distinguish published corrections from the latest working-tree changes.

## Architecture and maintainability assessment

The per-camera I/O and shared-inference topology is appropriate for the intended workload. The storage layout, hardware variant selection and model tags provide useful central contracts. Fresh migrations, lock consistency and static checks give a sound baseline for making the corrections.

The recurring weakness is ownership across asynchronous boundaries: a track is forgotten before a write, a scene advances before commit, a settings value is declared selected before all producers adopt it, and a page assumes cleanup has covered resources created later. Correcting these state transitions is more valuable than broad cosmetic refactoring.

The largest service entrypoints contain orchestration, mutable state and persistence together. Pending finalizations and API face activation now have dedicated owners in the lifecycle bundle described above. Track transitions share an immutable core contract and one persistence implementation. Further extraction should follow explicit lifecycle and persistence boundaries, preserving one authoritative implementation without compatibility layers or duplicate stores.

The shared-memory writer already invalidates a slot before overwriting pixels and publishes completed metadata afterward. The review did not establish a new torn-frame defect there. Similarly, model-tag filtering is a valid protection against cross-model face comparisons; finding 06 concerns activation coordination around that protection.

## Limits of the conclusion

The findings describe the reviewed checkout and reproducible failure conditions. They do not establish how often any defect has occurred in production. Live configuration can override file defaults; the production model and recording checks above describe the verified deployment snapshot, not an inference from defaults or a hardware performance assessment.

Target checks now include production OpenVINO/VAAPI startup evidence and an isolated OSNet CUDA execution. They do not measure learned-model quality, camera reconnect behaviour under injected network loss, recording throughput or disk-failure recovery. Fresh migration success does not prove every upgrade path or restoration of a populated database. Required publication gates executed dependency CVE and secret scans for the published corrections; a complete container-image vulnerability and weights-license audit remains outside the executed checks.

The review is complete as a repository-wide source assessment with targeted verification. It is not an end-to-end production acceptance result. The report is the canonical record for these findings; project memory should link here rather than maintain a second copy of the review.
