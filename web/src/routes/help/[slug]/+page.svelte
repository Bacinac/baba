<script lang="ts">
  import { page } from "$app/state";
  import { HelpPage } from "$lib/kit";
  import { help } from "$lib/help";
</script>

<HelpPage {help} slug={page.params.slug ?? ""} />
