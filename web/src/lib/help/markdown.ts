// A deliberately tiny markdown → HTML renderer for the in-app help articles.
//
// The frontend avoids heavy libraries on principle, and a full markdown parser
// is more than the help needs: the article content is authored in this repo
// (help/content.ts), never user-supplied, so a constrained subset is safe and
// enough. Supported: ## / ### headings, paragraphs, **bold**, `code`, bullet
// (`- `) and numbered (`1. `) lists, > blockquotes, [text](url) links, and a
// `---` rule.
//
// Everything is HTML-escaped BEFORE any markup is applied, so even though the
// input is trusted the output can't inject — the inline transforms only ever
// insert their own fixed tags.

function esc(s: string): string {
  return s
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function inline(s: string): string {
  // Order matters: escape first, then re-introduce our own safe tags.
  let out = esc(s);
  // `code` — before bold/links so backtick spans are literal.
  out = out.replace(/`([^`]+)`/g, '<code class="rounded bg-baba-panel-2 px-1 py-0.5 text-[0.85em]">$1</code>');
  // [text](url) — url is attribute-escaped by esc() already; only allow a safe
  // scheme set so a stray authoring slip can't produce javascript: links.
  out = out.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_m, text, url) => {
    const safe = /^(\/|https?:\/\/|mailto:)/.test(url) ? url : "#";
    const ext = safe.startsWith("http");
    return `<a href="${safe}"${ext ? ' target="_blank" rel="noopener"' : ""} class="text-baba-accent hover:underline">${text}</a>`;
  });
  // **bold**
  out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  return out;
}

export function renderMarkdown(md: string): string {
  const blocks = md.trim().split(/\n{2,}/);
  const html: string[] = [];
  for (const raw of blocks) {
    const block = raw.trim();
    if (!block) continue;
    if (block === "---") {
      html.push('<hr class="my-6 border-baba-border" />');
      continue;
    }
    if (block.startsWith("### ")) {
      html.push(`<h3 class="mt-6 mb-2 text-l font-semibold">${inline(block.slice(4))}</h3>`);
      continue;
    }
    if (block.startsWith("## ")) {
      html.push(`<h2 class="mt-8 mb-3 text-xl font-semibold">${inline(block.slice(3))}</h2>`);
      continue;
    }
    const lines = block.split("\n");
    if (lines.every((l) => /^-\s+/.test(l))) {
      const items = lines.map((l) => `<li>${inline(l.replace(/^-\s+/, ""))}</li>`).join("");
      html.push(`<ul class="my-3 ml-5 list-disc space-y-1">${items}</ul>`);
      continue;
    }
    if (lines.every((l) => /^\d+\.\s+/.test(l))) {
      const items = lines.map((l) => `<li>${inline(l.replace(/^\d+\.\s+/, ""))}</li>`).join("");
      html.push(`<ol class="my-3 ml-5 list-decimal space-y-1">${items}</ol>`);
      continue;
    }
    if (lines.every((l) => l.startsWith(">"))) {
      const inner = inline(lines.map((l) => l.replace(/^>\s?/, "")).join(" "));
      html.push(
        `<blockquote class="my-3 border-l-2 border-baba-accent/50 pl-3 text-baba-text-muted">${inner}</blockquote>`,
      );
      continue;
    }
    html.push(`<p class="my-3 leading-relaxed">${inline(lines.join(" "))}</p>`);
  }
  return html.join("\n");
}
