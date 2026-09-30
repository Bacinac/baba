<script lang="ts">
  // app.css is imported by the root +layout, which always wraps this one.
  let { children } = $props();
</script>

<!-- Standalone layout: no sidebar, no auth gate. Just the form. -->
<div class="h-full">
  {@render children()}
</div>
