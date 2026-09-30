// Live feeds use Server-Sent Events (EventSource), not WebSockets. A browser WS
// upgrade doesn't survive the Cloudflare tunnel (same reason live video is HTTP
// fMP4), so the detection overlay was black behind the tunnel. SSE is plain
// long-lived HTTP GET and passes straight through; the session cookie rides
// along same-origin.

const GRACE_MS = 2000;
const RETRY_MIN_MS = 1000;
const RETRY_MAX_MS = 30000;

class LiveState {
  #down = new Set<symbol>();
  #timer: ReturnType<typeof setTimeout> | null = null;

  /** a live feed has been cut for longer than a reconnect takes */
  lost = $state(false);

  drop(id: symbol) {
    this.#down.add(id);
    this.#timer ??= setTimeout(() => {
      this.#timer = null;
      this.lost = this.#down.size > 0;
    }, GRACE_MS);
  }

  back(id: symbol) {
    this.#down.delete(id);
    if (this.#down.size > 0) return;
    this.lost = false;
    if (this.#timer) clearTimeout(this.#timer);
    this.#timer = null;
  }
}

export const live = new LiveState();

export type FeedHandlers<T> = {
  message: (data: T) => void;
  /** the feed came back after a drop: whatever was pushed meanwhile is gone,
   *  so fetch it. A feed whose next message is the whole state needs none. */
  resync?: () => void;
};

export type Feed = { close: () => void };

export function liveFeed<T>(url: string, on: FeedHandlers<T>): Feed {
  const id = Symbol(url);
  let es: EventSource | null = null;
  let retry: ReturnType<typeof setTimeout> | null = null;
  let wait = RETRY_MIN_MS;
  let dropped = false;

  function open() {
    retry = null;
    es = new EventSource(url);
    es.onopen = () => {
      wait = RETRY_MIN_MS;
      live.back(id);
      if (dropped) {
        dropped = false;
        on.resync?.();
      }
    };
    es.onmessage = (ev) => {
      let data: T;
      try {
        data = JSON.parse(ev.data) as T;
      } catch (e) {
        console.error("live feed decode", url, e);
        return;
      }
      on.message(data);
    };
    es.onerror = () => {
      dropped = true;
      live.drop(id);
      // The browser retries a stream that broke off, but a reply that is not a
      // stream at all — the tunnel's 502 while the api restarts — closes the
      // EventSource for good, and the page would stay dead until reloaded.
      if (es?.readyState === EventSource.CLOSED) {
        es.close();
        retry = setTimeout(open, wait);
        wait = Math.min(wait * 2, RETRY_MAX_MS);
      }
    };
  }

  open();
  return {
    close() {
      if (retry) clearTimeout(retry);
      retry = null;
      es?.close();
      live.back(id);
    },
  };
}
