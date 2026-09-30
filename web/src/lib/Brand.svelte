<script lang="ts">
  import logo from "$lib/baba-logo.svg";

  // Without a size the logo takes its height from where it stands: the kit's frame
  // and about page size every product's mark alike.
  let { size }: { size?: "md" } = $props();
</script>

<img src={logo} alt="BABA" class="block h-auto" style:width={size && "168px"} />
