<script lang="ts">
  // Pipeline thresholds, generated from the server's catalogue.
  //
  // There is deliberately no hand-written list of fields here. Every knob the
  // api returns gets a row, with its own range enforced, so adding one in
  // `baba_core.tunables` surfaces it in this form with no change to this file.
  // A hand-kept list would be the second copy the whole exercise removes.
  //
  // These were env-only until now: to move one you edited `.env` and
  // redeployed. They are the values that decide what BABA sees, so they were
  // exactly the settings the operator could not reach.
  import { onMount } from "svelte";
  import { api, type Tunable } from "$lib/api";
  import { Button, Card, formatNumber, Notice, toasts, SaveButton } from "$lib/kit";
  import { t, type MessageKey } from "$lib/i18n";

  let rows = $state<Tunable[]>([]);
  let edited = $state<Record<string, string>>({});
  let loading = $state(true);
  let saving = $state(false);
  let error = $state<string | null>(null);

  async function refresh() {
    loading = true;
    error = null;
    try {
      rows = await api.listTunables();
      edited = {};
    } catch (e) {
      error = (e as Error).message;
    } finally {
      loading = false;
    }
  }
  onMount(refresh);

  // Group headings come from the api's group key, so a new group needs one
  // i18n entry and nothing else.
  let groups = $derived.by(() => {
    const by = new Map<string, Tunable[]>();
    for (const r of rows) {
      const list = by.get(r.group) ?? [];
      list.push(r);
      by.set(r.group, list);
    }
    return [...by.entries()];
  });

  let dirty = $derived(Object.keys(edited).length > 0);

  function label(knob: Tunable): string {
    // The catalogue names the key; if a translation is missing we show the
    // raw knob name rather than a blank row.
    const v = t(knob.help_key as MessageKey);
    return v === knob.help_key ? knob.name : v;
  }

  // Thresholds run from 0.05 to 2 592 000 in the same list: two decimals keep
  // 0.05 and print a whole number whole.
  function num(v: number): string {
    return formatNumber(v, { maximumFractionDigits: 2 });
  }

  function onInput(name: string, raw: string) {
    edited = { ...edited, [name]: raw };
  }

  function reset(name: string) {
    // Explicit null = clear the operator value, fall back to the deployment
    // default. Distinct from typing the default in, which stores it.
    edited = { ...edited, [name]: "" };
  }

  async function save() {
    saving = true;
    error = null;
    try {
      const values: Record<string, number | null> = {};
      for (const [name, raw] of Object.entries(edited)) {
        const trimmed = raw.trim();
        if (trimmed === "") {
          values[name] = null;
          continue;
        }
        const n = Number(trimmed);
        if (!Number.isFinite(n)) {
          error = t("tun_err_not_a_number");
          return;
        }
        values[name] = n;
      }
      rows = await api.putTunables(values);
      edited = {};
      toasts.success(t("tun_saved"));
    } catch (e) {
      error = (e as Error).message;
    } finally {
      saving = false;
    }
  }
</script>

<Card title={t("tun_title")}>
  <p class="text-s text-baba-text-faint">{t("tun_desc")}</p>

  {#if loading}
    <p class="muted text-m">{t("tun_loading")}</p>
  {:else}
    <div class="space-y-5">
      {#each groups as [group, items] (group)}
        <div>
          <h4 class="mb-2 text-s uppercase tracking-wide text-baba-text-faint">
            {t(`tun_group_${group}` as MessageKey)}
          </h4>
          <div class="space-y-1.5">
            {#each items as item (item.name)}
              {@const pending = edited[item.name]}
              {@const shown = pending !== undefined ? pending : (item.is_set ? String(item.value) : "")}
              <div class="grid grid-cols-[1fr_7rem_auto] items-center gap-3 rounded border border-baba-border bg-baba-bg/40 px-3 py-1.5">
                <div class="min-w-0">
                  <div class="truncate text-m">{label(item)}</div>
                  <div class="text-xs text-baba-text-faint tabular-nums">
                    {t("tun_default")}: {num(item.default)}
                    · {num(item.lo)}–{num(item.hi)}
                    {#if !item.is_set && pending === undefined}
                      · {t("tun_inherited")}
                    {/if}
                  </div>
                </div>
                <input
                  class="w-full text-right tabular-nums"
                  type="number"
                  min={item.lo}
                  max={item.hi}
                  step="any"
                  placeholder={String(item.default)}
                  value={shown}
                  oninput={(e) => onInput(item.name, e.currentTarget.value)}
                />
                <Button size="small"
                  disabled={!item.is_set && pending === undefined}
                  onclick={() => reset(item.name)}
                >
                  {t("tun_reset")}
                </Button>
              </div>
            {/each}
          </div>
        </div>
      {/each}
    </div>

    <div class="mt-4 flex items-center gap-3">
      <SaveButton {dirty} {saving} onclick={save} />
      {#if error}
        <Notice tone="err">{error}</Notice>
      {/if}
    </div>
  {/if}
</Card>
