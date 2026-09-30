<script lang="ts">
  import { formatNumber, Button, Card, Tag } from "$lib/kit";
  import { t } from "$lib/i18n";
  import { api, type Camera, type DiscoveredCamera, type DiscoverResult, type ProbeResult, type StreamCandidate } from "$lib/api";

  type Props = {
    onCameraAdded: () => void;
    existingByHost?: Map<string, Camera>;
  };
  let { onCameraAdded, existingByHost }: Props = $props();

  let subnet = $state("192.168.1.0/24");
  let scanning = $state(false);
  let result = $state<DiscoverResult | null>(null);
  let scanError = $state<string | null>(null);

  // One camera expanded at a time. Inside the expanded section:
  //   1. user types username + password
  //   2. clicks "Find stream" → POST /cameras/probe
  //   3. backend tries ONVIF first, then vendor templates; returns
  //      candidates with snapshots
  //   4. user clicks a candidate → POST /cameras/quickadd → navigate
  let expandedIp = $state<string | null>(null);
  let username = $state("admin");
  let password = $state("");
  let probing = $state(false);
  let probeError = $state<string | null>(null);
  let probeResult = $state<ProbeResult | null>(null);
  let addingUrl = $state<string | null>(null);

  function reset() {
    username = "admin";
    password = "";
    probing = false;
    probeError = null;
    probeResult = null;
    addingUrl = null;
  }

  async function scan(e?: SubmitEvent) {
    e?.preventDefault();
    scanning = true;
    scanError = null;
    result = null;
    expandedIp = null;
    reset();
    try {
      result = await api.discoverCameras(subnet);
    } catch (err) {
      scanError = (err as Error).message;
    } finally {
      scanning = false;
    }
  }

  function expand(cam: DiscoveredCamera) {
    if (expandedIp === cam.ip) {
      expandedIp = null;
      reset();
      return;
    }
    expandedIp = cam.ip;
    reset();
  }

  async function probe() {
    if (!expandedIp || probing) return;
    probing = true;
    probeError = null;
    probeResult = null;
    try {
      probeResult = await api.probeCamera(expandedIp, username, password);
    } catch (e) {
      probeError = (e as Error).message;
    } finally {
      probing = false;
    }
  }

  // Largest pixel-area candidate = mainstream; smallest (if strictly smaller)
  // = substream. Used to label each card and to auto-pair on add.
  function pixelArea(c: StreamCandidate): number {
    return (c.width ?? 0) * (c.height ?? 0);
  }
  let mainCandidate = $derived.by(() => {
    if (!probeResult || probeResult.candidates.length === 0) return null;
    return probeResult.candidates.reduce((a, b) => pixelArea(b) > pixelArea(a) ? b : a);
  });
  let subCandidate = $derived.by(() => {
    if (!probeResult || !mainCandidate || probeResult.candidates.length < 2) return null;
    const others = probeResult.candidates.filter(c => c !== mainCandidate);
    const smallest = others.reduce((a, b) => pixelArea(b) < pixelArea(a) ? b : a);
    return pixelArea(smallest) < pixelArea(mainCandidate) ? smallest : null;
  });

  async function addCandidate(cand: StreamCandidate) {
    if (!expandedIp || addingUrl) return;
    addingUrl = cand.stream_url;
    // If user clicked the main candidate and a sub exists, pair them. If they
    // clicked the sub (or an "other" mid-tier candidate), respect that as a
    // single-stream choice — they're opting out of dual-stream deliberately.
    const sub = (cand === mainCandidate && subCandidate) ? subCandidate.stream_url : null;
    try {
      await api.quickaddCamera({
        stream_url: cand.stream_url,
        substream_url: sub,
        ip: expandedIp,
      });
      // Stay on this page; the new camera will show up as "Already added"
      // in the scan list and the user can keep adding from the same scan.
      expandedIp = null;
      reset();
      onCameraAdded();
    } catch (e) {
      probeError = (e as Error).message;
      addingUrl = null;
    }
  }
</script>

<Card title={t("discover_title")}>

  <form class="mt-3 flex items-end gap-3" onsubmit={scan}>
    <div class="flex-1 min-w-[14rem]">
      <label class="block text-s text-baba-text-muted" for="subnet">
        {t("discover_subnet_label")}
      </label>
      <input
        id="subnet"
        type="text"
        bind:value={subnet}
        required
        spellcheck="false"
        autocomplete="off"
        class="mt-1 w-full rounded border border-baba-border bg-baba-panel-2 px-2 py-1.5 text-m font-mono focus:border-baba-accent focus:outline-none"
        placeholder={t("discover_subnet_placeholder")}
      />
    </div>
    <Button tone="primary" type="submit" disabled={scanning}>{scanning ? t("discover_scanning") : t("discover_scan")}</Button>
  </form>

  {#if scanError}<p class="mt-3 text-m text-red-400">{scanError}</p>{/if}

  {#if result}
    <p class="mt-3 text-s text-baba-text-faint">
      {t("discover_found")} {formatNumber(result.found.length, { maximumFractionDigits: 1 })} ·
      {formatNumber(result.scanned, { maximumFractionDigits: 1 })} {t("discover_scanned")} {formatNumber(result.duration_ms / 1000, { maximumFractionDigits: 1 })} s
    </p>

    {#if result.found.length === 0}
      <p class="mt-2 text-m text-baba-text-faint">{t("discover_no_results")}</p>
    {:else}
      <ul class="mt-3 divide-y divide-baba-border overflow-hidden rounded border border-baba-border">
        {#each result.found as cam (cam.ip)}
          {@const existing = existingByHost?.get(cam.ip)}
          <li class="bg-baba-panel-2" class:opacity-60={existing}>
            <!-- header row -->
            <div class="flex items-center justify-between gap-3 px-3 py-2 text-m">
              <div class="min-w-0 flex-1">
                <div class="flex items-center gap-2">
                  <span class="font-mono">{cam.ip}</span>
                  {#each cam.signals as sig (sig)}
                    <span class="font-mono"><Tag tone="ok">
                      {sig}
                    </Tag></span>
                  {/each}
                  {#if existing}
                    <Tag tone="warn">
                      {t("discover_already_added")}{existing.name ? ` · ${existing.name}` : ""}
                    </Tag>
                  {/if}
                </div>
                <div class="mt-0.5 text-s text-baba-text-faint">
                  {t("discover_open_ports")}: {cam.open_ports.join(", ")}
                </div>
              </div>
              {#if existing}
                <a
                  href={`/settings/cameras/${existing.id}`}
                  class="shrink-0 rounded border border-baba-border bg-baba-panel px-2 py-1 text-s text-baba-text-muted hover:bg-baba-bg"
                >{t("discover_open_settings")}</a>
              {:else}
                <div class="shrink-0"><Button size="small" selected={expandedIp === cam.ip} onclick={() => expand(cam)}>{expandedIp === cam.ip ? t("discover_cancel") : t("discover_connect")}</Button></div>
              {/if}
            </div>

            <!-- expanded: creds + probe + candidates -->
            {#if expandedIp === cam.ip && !existing}
              <div class="space-y-3 border-t border-baba-border bg-baba-panel px-3 pb-3 pt-3">
                <!-- creds form -->
                <div class="flex flex-wrap items-end gap-3">
                  <label class="block w-40">
                    <span class="block text-s text-baba-text-muted mb-1">{t("probe_username")}</span>
                    <input
                      type="text"
                      bind:value={username}
                      autocomplete="off" spellcheck="false"
                      class="w-full rounded border border-baba-border bg-baba-panel-2 px-2 py-1.5 text-m font-mono focus:border-baba-accent focus:outline-none"
                    />
                  </label>
                  <label class="block w-48">
                    <span class="block text-s text-baba-text-muted mb-1">{t("probe_password")}</span>
                    <input
                      type="password"
                      bind:value={password}
                      autocomplete="new-password"
                      class="w-full rounded border border-baba-border bg-baba-panel-2 px-2 py-1.5 text-m font-mono focus:border-baba-accent focus:outline-none"
                    />
                  </label>
                  <Button tone="primary" onclick={probe} disabled={probing}>{probing ? t("probe_probing") : t("probe_button")}</Button>
                </div>

                {#if probeError}
                  <p class="text-m text-red-400">{probeError}</p>
                {/if}

                {#if probeResult}
                  {#if probeResult.vendor || probeResult.model}
                    <p class="text-s text-baba-text-faint">
                      {t("probe_vendor")}:
                      <span class="text-baba-text-muted">{probeResult.vendor ?? "?"} {probeResult.model ?? ""}</span>
                    </p>
                  {/if}

                  {#if probeResult.candidates.length === 0}
                    <p class="text-m text-baba-text-faint">{t("probe_no_candidates")}</p>
                  {:else}
                    <div class="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
                      {#each probeResult.candidates as cand (cand.stream_url)}
                        {@const isMain = cand === mainCandidate}
                        {@const isSub = cand === subCandidate}
                        <div class="overflow-hidden rounded border border-baba-border bg-baba-bg">
                          {#if cand.snapshot_b64}
                            <img
                              src={cand.snapshot_b64}
                              alt={cand.label}
                              class="block aspect-video w-full bg-black object-contain"
                            />
                          {:else}
                            <div class="grid aspect-video w-full place-items-center bg-black text-s text-baba-text-faint">
                              {t("probe_no_preview")}
                            </div>
                          {/if}
                          <div class="space-y-1 p-2">
                            <div class="flex items-center gap-2">
                              <div class="text-m font-medium">{cand.label}</div>
                              {#if isMain && subCandidate}
                                <Tag tone="warn">
                                  {t("probe_role_main")}
                                </Tag>
                              {:else if isSub}
                                <Tag tone="busy">
                                  {t("probe_role_sub")}
                                </Tag>
                              {/if}
                            </div>
                            <div class="text-xs text-baba-text-faint">
                              {cand.codec ?? "?"} · {cand.width ?? "?"}×{cand.height ?? "?"}
                              {#if cand.fps}· {formatNumber(cand.fps, { maximumFractionDigits: 1 })} {t("stream_fps")}{/if}
                            </div>
                            <div class="mt-1 grid"><Button tone="primary" size="small" onclick={() => addCandidate(cand)} disabled={addingUrl !== null}>{addingUrl === cand.stream_url ? t("probe_adding") : (isMain && subCandidate ? t("probe_add_camera_paired") : t("probe_add_camera"))}</Button></div>
                          </div>
                        </div>
                      {/each}
                    </div>
                  {/if}
                {/if}
              </div>
            {/if}
          </li>
        {/each}
      </ul>
    {/if}
  {/if}
</Card>
