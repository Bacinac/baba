<script lang="ts">
  import { page } from "$app/state";
  import { HelpPage } from "#lib/kit/index.js";
  import { help } from "#lib/help.js";
</script>

<HelpPage {help} slug={page.params.slug ?? ""} />
