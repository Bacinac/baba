<script lang="ts">
  // Settings root redirects to the first sub-page (Cameras). This way the
  // sub-nav always has a selected item and `/settings` isn't an awkward dead end.
  import { goto } from "$app/navigation";
  import { onMount } from "svelte";
  onMount(() => goto("/settings/cameras", { replaceState: true }));
</script>
