<script lang="ts">
  import { byLabel } from "$lib/order";
  import { onMount } from "svelte";
  import { formatNumber, Button } from "$lib/kit";
  import { t, type MessageKey } from "$lib/i18n";
  import { api, type TestConnectionResult } from "$lib/api";
  import {
    recogniseStreams,
    streamsFor,
    type AnalysisStream,
    type CameraStreams,
    type StreamChoice,
    type StreamPreset,
  } from "$lib/streamPresets";

  type Props = {
    streamUrl: string;
    substreamUrl: string | null;
    analysisStream: AnalysisStream;
    onChange: (v: CameraStreams) => void;
    /** Disable inputs + test while busy (e.g. saving). */
    disabled?: boolean;
  };
  let { streamUrl, substreamUrl, analysisStream, onChange, disabled = false }: Props = $props();

  const NOTES: Partial<Record<string, MessageKey>> = {
    reolink_flv: "stream_preset_note_reolink_flv",
    onvif: "stream_preset_note_onvif",
  };

  let presets = $state<StreamPreset[] | null>(null);
  let presetsError = $state<string | null>(null);

  // The credentials belong to the camera, not to each URL: entered once and
  // shared by both streams.
  let ip = $state("");
  let user = $state("");
  let pass = $state("");
  let showPass = $state(false);
  let presetId = $state("");
  let mainPath = $state("");
  let subPath = $state("");
  let choice = $state<StreamChoice>("main");
  let analysis = $state<AnalysisStream>("main");
  // Escape hatch: a camera whose URLs no preset describes is edited verbatim.
  let raw = $state(false);
  let rawMain = $state("");
  let rawSub = $state("");

  let testing = $state(false);
  let tests = $state<{ label: MessageKey; result: TestConnectionResult }[]>([]);

  // What we last handed the parent, so the re-hydrate effect below can tell an
  // external prop change (async load, form reset) from the value flowing back
  // after our own edit — and not clobber the fields on the round trip.
  let last: CameraStreams | null = null;

  const preset = $derived(presets?.find(p => p.id === presetId) ?? null);
  const hasSub = $derived(!!preset?.sub);
  const needsPath = $derived(!!preset?.main.includes("{path}"));
  const parts = $derived({ ip, user, pass, path: mainPath });

  const current = $derived.by((): CameraStreams => {
    if (raw) {
      const sub = rawSub.trim() || null;
      return { stream_url: rawMain, substream_url: sub, analysis_stream: sub ? analysis : "main" };
    }
    if (!preset) return { stream_url: "", substream_url: null, analysis_stream: "main" };
    return streamsFor(preset, parts, subPath, hasSub ? choice : "main", analysis);
  });

  const shown = $derived.by(() => {
    if (raw || !preset) return null;
    return streamsFor(preset, { ...parts, pass: showPass ? pass : "***" }, subPath, hasSub ? choice : "main", analysis);
  });

  const recognisedRaw = $derived(
    raw && presets ? recogniseStreams(presets, rawMain, rawSub.trim() || null) : null,
  );

  onMount(async () => {
    try {
      presets = await api.streamPresets();
    } catch (e) {
      presetsError = (e as Error).message;
    }
  });

  function hydrate(list: readonly StreamPreset[]) {
    const r = recogniseStreams(list, streamUrl, substreamUrl);
    rawMain = streamUrl;
    rawSub = substreamUrl ?? "";
    analysis = analysisStream;
    if (r) {
      presetId = r.presetId;
      ip = r.parts.ip;
      user = r.parts.user;
      pass = r.parts.pass;
      mainPath = r.parts.path;
      subPath = r.subPath;
      choice = r.choice;
      raw = false;
    } else {
      raw = !!streamUrl || list.length === 0;
      choice = substreamUrl ? "both" : "main";
    }
    last = { stream_url: streamUrl, substream_url: substreamUrl, analysis_stream: analysisStream };
  }

  $effect(() => {
    if (!presets && !presetsError) return;
    if (
      !last
      || streamUrl !== last.stream_url
      || substreamUrl !== last.substream_url
      || analysisStream !== last.analysis_stream
    ) {
      hydrate(presets ?? []);
    }
  });

  function emit() {
    tests = [];
    last = current;
    onChange(current);
  }

  function pickPreset(id: string) {
    presetId = id;
    if (!presets?.find(p => p.id === id)?.sub) choice = "main";
    emit();
  }

  function toggleRaw() {
    if (raw) {
      const r = recognisedRaw;
      if (r) {
        presetId = r.presetId;
        ip = r.parts.ip;
        user = r.parts.user;
        pass = r.parts.pass;
        mainPath = r.parts.path;
        subPath = r.subPath;
        choice = r.choice;
      }
      raw = false;
      emit();
    } else {
      rawMain = current.stream_url;
      rawSub = current.substream_url ?? "";
      raw = true;
    }
  }

  async function runTest() {
    if (!current.stream_url || testing || disabled) return;
    const targets: { label: MessageKey; url: string }[] = [
      { label: !raw && choice === "sub" ? "camera_streams_sub" : "camera_streams_main", url: current.stream_url },
    ];
    if (current.substream_url) targets.push({ label: "camera_streams_sub", url: current.substream_url });
    testing = true;
    tests = [];
    try {
      for (const t of targets) {
        let result: TestConnectionResult;
        try {
          result = await api.testConnection(t.url);
        } catch (e) {
          result = {
            ok: false,
            codec: null, width: null, height: null, fps: null,
            duration_ms: 0,
            error: (e as Error).message,
          };
        }
        tests = [...tests, { label: t.label, result }];
      }
    } finally {
      testing = false;
    }
  }

  const fieldCls =
    "mt-1 w-full rounded border border-baba-border bg-baba-panel-2 px-2 py-1.5 text-m focus:border-baba-accent focus:outline-none disabled:opacity-50";
</script>

<div class="space-y-3">
  {#if presetsError}
    <p class="text-s text-red-400">{t("stream_presets_failed")} {presetsError}</p>
  {/if}

  {#if raw}
    <div>
      <label class="block text-s text-baba-text-muted" for="stream-url-raw">
        {t("add_camera_stream_url")}
      </label>
      <input
        id="stream-url-raw"
        type="text"
        value={rawMain}
        oninput={(e) => { rawMain = (e.currentTarget as HTMLInputElement).value; emit(); }}
        {disabled}
        required
        spellcheck="false"
        autocomplete="off"
        class="{fieldCls} font-mono"
        placeholder="rtsp://user:pass@192.168.1.20:554/stream"
      />
    </div>
    <div>
      <label class="block text-s text-baba-text-muted" for="substream-url-raw">
        {t("camera_substream_url")}
      </label>
      <input
        id="substream-url-raw"
        type="text"
        value={rawSub}
        oninput={(e) => { rawSub = (e.currentTarget as HTMLInputElement).value; emit(); }}
        {disabled}
        spellcheck="false"
        autocomplete="off"
        class="{fieldCls} font-mono"
        placeholder="rtsp://user:pass@192.168.1.20:554/substream"
      />
      <p class="mt-1 text-xs text-baba-text-faint">{t("camera_substream_url_hint")}</p>
    </div>
    {#if rawSub.trim()}
      <div class="w-48">
        <label class="block text-s text-baba-text-muted" for="analysis-stream-raw">
          {t("camera_analysis_stream")}
        </label>
        <select
          id="analysis-stream-raw"
          value={analysis}
          onchange={(e) => { analysis = (e.currentTarget as HTMLSelectElement).value as AnalysisStream; emit(); }}
          {disabled}
          class={fieldCls}
        >
          <option value="main">{t("camera_streams_main")}</option>
          <option value="sub">{t("camera_streams_sub")}</option>
        </select>
      </div>
      <p class="text-xs text-baba-text-faint">{t("camera_analysis_stream_hint")}</p>
    {/if}
  {:else}
    <div class="grid grid-cols-2 gap-3 md:grid-cols-4">
      <div class="col-span-2">
        <label class="block text-s text-baba-text-muted" for="cam-ip">
          {t("add_camera_ip")}
        </label>
        <input
          id="cam-ip"
          type="text"
          value={ip}
          oninput={(e) => { ip = (e.currentTarget as HTMLInputElement).value; emit(); }}
          {disabled}
          spellcheck="false"
          autocomplete="off"
          class="{fieldCls} font-mono"
          placeholder={t("add_camera_ip_placeholder")}
        />
      </div>
      <div>
        <label class="block text-s text-baba-text-muted" for="cam-user">
          {t("add_camera_user")}
        </label>
        <input
          id="cam-user"
          type="text"
          value={user}
          oninput={(e) => { user = (e.currentTarget as HTMLInputElement).value; emit(); }}
          {disabled}
          spellcheck="false"
          autocomplete="off"
          class={fieldCls}
          placeholder="admin"
        />
      </div>
      <div>
        <label class="block text-s text-baba-text-muted" for="cam-pass">
          {t("add_camera_password")}
        </label>
        <div class="relative">
          <input
            id="cam-pass"
            type={showPass ? "text" : "password"}
            value={pass}
            oninput={(e) => { pass = (e.currentTarget as HTMLInputElement).value; emit(); }}
            {disabled}
            spellcheck="false"
            autocomplete="off"
            class="{fieldCls} pr-14"
          />
          <button
            type="button"
            onclick={() => (showPass = !showPass)}
            tabindex="-1"
            class="absolute inset-y-0 right-1 my-1 rounded px-1.5 text-xs text-baba-text-muted hover:text-baba-text"
          >{showPass ? t("camera_password_hide") : t("camera_password_show")}</button>
        </div>
      </div>
    </div>

    <div class="flex flex-wrap items-end gap-3">
      <div class="w-64">
        <label class="block text-s text-baba-text-muted" for="preset">
          {t("add_camera_preset")}
        </label>
        <select
          id="preset"
          value={presetId}
          onchange={(e) => pickPreset((e.currentTarget as HTMLSelectElement).value)}
          {disabled}
          class={fieldCls}
        >
          <option value="">{t("add_camera_preset_pick")}</option>
          {#each byLabel(presets ?? [], (p) => p.label) as p (p.id)}
            <option value={p.id}>{p.label}</option>
          {/each}
        </select>
      </div>

      <div class="w-40">
        <label class="block text-s text-baba-text-muted" for="stream-choice">
          {t("camera_streams")}
        </label>
        <select
          id="stream-choice"
          value={hasSub ? choice : "main"}
          onchange={(e) => { choice = (e.currentTarget as HTMLSelectElement).value as StreamChoice; emit(); }}
          disabled={disabled || !hasSub}
          title={preset && !hasSub ? t("camera_streams_no_sub") : undefined}
          class={fieldCls}
        >
          <option value="main">{t("camera_streams_main")}</option>
          <option value="sub">{t("camera_streams_sub")}</option>
          <option value="both">{t("camera_streams_both")}</option>
        </select>
      </div>

      {#if hasSub && choice === "both"}
        <div class="w-40">
          <label class="block text-s text-baba-text-muted" for="analysis-stream">
            {t("camera_analysis_stream")}
          </label>
          <select
            id="analysis-stream"
            value={analysis}
            onchange={(e) => { analysis = (e.currentTarget as HTMLSelectElement).value as AnalysisStream; emit(); }}
            {disabled}
            class={fieldCls}
          >
            <option value="main">{t("camera_streams_main")}</option>
            <option value="sub">{t("camera_streams_sub")}</option>
          </select>
        </div>
      {/if}
    </div>

    {#if needsPath}
      <div class="flex flex-wrap gap-3">
        {#if choice !== "sub"}
          <div class="min-w-[12rem] flex-1">
            <label class="block text-s text-baba-text-muted" for="main-path">
              {t("add_camera_path")}
            </label>
            <input
              id="main-path"
              type="text"
              value={mainPath}
              oninput={(e) => { mainPath = (e.currentTarget as HTMLInputElement).value; emit(); }}
              {disabled}
              spellcheck="false"
              autocomplete="off"
              class="{fieldCls} font-mono"
              placeholder="cam/stream1"
            />
          </div>
        {/if}
        {#if choice !== "main"}
          <div class="min-w-[12rem] flex-1">
            <label class="block text-s text-baba-text-muted" for="sub-path">
              {t("add_camera_sub_path")}
            </label>
            <input
              id="sub-path"
              type="text"
              value={subPath}
              oninput={(e) => { subPath = (e.currentTarget as HTMLInputElement).value; emit(); }}
              {disabled}
              spellcheck="false"
              autocomplete="off"
              class="{fieldCls} font-mono"
              placeholder="cam/stream2"
            />
          </div>
        {/if}
      </div>
    {/if}

    {#if hasSub && choice === "both"}
      <p class="text-xs text-baba-text-faint">{t("camera_analysis_stream_hint")}</p>
    {/if}

    {#if preset && NOTES[preset.id]}
      <p class="text-s text-baba-text-faint">{t(NOTES[preset.id] as MessageKey)}</p>
    {/if}

    {#if shown}
      <div class="space-y-0.5 break-all font-mono text-xs text-baba-text-faint">
        <p>{shown.stream_url}</p>
        {#if shown.substream_url}<p>{shown.substream_url}</p>{/if}
      </div>
    {/if}
  {/if}

  <div class="flex flex-wrap items-center gap-3">
    <Button onclick={runTest} disabled={!current.stream_url || testing || disabled}>{testing ? t("camera_test_testing") : t("camera_test_connection")}</Button>
    <Button size="small" onclick={toggleRaw} disabled={disabled || (raw && !!rawMain.trim() && !recognisedRaw)} title={raw && !!rawMain.trim() && !recognisedRaw ? t("add_camera_builder_unmatched") : undefined}>{raw ? t("add_camera_use_builder") : t("add_camera_raw_toggle")}</Button>
  </div>

  {#each tests as test (test.label)}
    {#if test.result.ok}
      <div class="rounded border border-emerald-500/30 bg-emerald-500/5 px-3 py-2 text-s">
        <span class="font-medium text-emerald-400">✓ {t(test.label)} · {t("camera_test_ok")}</span>
        <span class="ml-2 text-baba-text-muted">
          {t("stream_codec")}={test.result.codec ?? "?"}
          · {test.result.width ?? "?"}×{test.result.height ?? "?"}
          {#if test.result.fps}· {formatNumber(test.result.fps, { maximumFractionDigits: 1 })} {t("stream_fps")}{/if}
          · {formatNumber(test.result.duration_ms, { maximumFractionDigits: 0 })} ms
        </span>
      </div>
    {:else}
      <div class="rounded border border-red-500/30 bg-red-500/5 px-3 py-2 text-s">
        <span class="font-medium text-red-400">✗ {t(test.label)} · {t("camera_test_failed")}</span>
        <span class="ml-2 text-baba-text-muted">{test.result.error ?? t("camera_test_unknown_error")}</span>
      </div>
    {/if}
  {/each}
</div>
