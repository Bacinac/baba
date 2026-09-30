// The URL shapes of the cameras we know come from the api (GET /stream-presets),
// which probes cameras with the same list. A preset is one vendor's main-stream
// template and, where the vendor has one, its substream template. Placeholders
// are {ip}, {user}, {pass}, and {path} for a free-form RTSP path; the form
// collects them as fields, so one set of credentials builds both streams.

export interface StreamPreset {
  id: string;
  label: string;
  main: string;
  sub: string | null;
}

export interface StreamParts {
  ip: string;
  user: string;
  pass: string;
  /** Only meaningful for a template with {path}; the rest bake the path in. */
  path: string;
}

/** Which of its streams a camera exposes. */
export type StreamChoice = "main" | "sub" | "both";
export type AnalysisStream = "main" | "sub";

export interface CameraStreams {
  stream_url: string;
  substream_url: string | null;
  analysis_stream: AnalysisStream;
}

// User and password are percent-encoded so a credential containing @ : / ? # &
// — which would otherwise break the URL structure or read as a query separator
// — survives intact both in RTSP userinfo and in the FLV query string.
export function assembleStreamUrl(template: string, p: StreamParts): string {
  const enc = (s: string) => encodeURIComponent(s);
  return template
    .replaceAll("{user}", enc(p.user))
    .replaceAll("{pass}", enc(p.pass))
    .replaceAll("{ip}", p.ip.trim())
    .replaceAll("{path}", p.path.trim().replace(/^\/+/, ""));
}

const FIELD: Record<string, string> = {
  "{ip}": "(?<ip>[^/:?#&@]+)",
  "{user}": "(?<user>[^:@/?#&]*)",
  "{pass}": "(?<pass>[^@/?#&]*)",
  "{path}": "(?<path>.*)",
};

// The inverse of assembleStreamUrl: the fields a stored URL was built from, or
// null when the template does not describe it. A stored value that is not
// valid percent-encoding is passed through verbatim rather than throwing.
export function readStreamUrl(template: string, url: string): StreamParts | null {
  const pattern = template
    .split(/(\{ip\}|\{user\}|\{pass\}|\{path\})/)
    .map(part => FIELD[part] ?? part.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))
    .join("");
  const g = new RegExp(`^${pattern}$`).exec(url)?.groups;
  if (!g) return null;
  const dec = (s: string) => {
    try { return decodeURIComponent(s); } catch { return s; }
  };
  return { ip: g.ip ?? "", user: dec(g.user ?? ""), pass: dec(g.pass ?? ""), path: g.path ?? "" };
}

export interface RecognisedStreams {
  presetId: string;
  parts: StreamParts;
  subPath: string;
  choice: StreamChoice;
}

// Match a camera's stored streams back to a preset. The api orders presets
// specific → generic, so a vendor's shape wins over the catch-all RTSP one.
export function recogniseStreams(
  presets: readonly StreamPreset[],
  streamUrl: string,
  substreamUrl: string | null,
): RecognisedStreams | null {
  if (!streamUrl) return null;
  for (const p of presets) {
    const main = readStreamUrl(p.main, streamUrl);
    if (substreamUrl) {
      const sub = p.sub ? readStreamUrl(p.sub, substreamUrl) : null;
      if (main && sub && main.ip === sub.ip && main.user === sub.user && main.pass === sub.pass) {
        return { presetId: p.id, parts: main, subPath: sub.path, choice: "both" };
      }
      continue;
    }
    if (main) return { presetId: p.id, parts: main, subPath: "", choice: "main" };
    const sub = p.sub ? readStreamUrl(p.sub, streamUrl) : null;
    if (sub) return { presetId: p.id, parts: { ...sub, path: "" }, subPath: sub.path, choice: "sub" };
  }
  return null;
}

// What the camera row stores for a choice: `stream_url` is what is recorded,
// so a camera that exposes only its substream records that.
export function streamsFor(
  preset: StreamPreset,
  parts: StreamParts,
  subPath: string,
  choice: StreamChoice,
  analysis: AnalysisStream,
): CameraStreams {
  const main = assembleStreamUrl(preset.main, parts);
  const sub = preset.sub ? assembleStreamUrl(preset.sub, { ...parts, path: subPath }) : null;
  if (choice === "sub" && sub) return { stream_url: sub, substream_url: null, analysis_stream: "main" };
  if (choice === "both" && sub) return { stream_url: main, substream_url: sub, analysis_stream: analysis };
  return { stream_url: main, substream_url: null, analysis_stream: "main" };
}
