import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { live, liveFeed } from "./live.svelte";

class FakeSource {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSED = 2;
  static made: FakeSource[] = [];

  readyState = FakeSource.CONNECTING;
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;

  constructor(public url: string) {
    FakeSource.made.push(this);
  }
  open() {
    this.readyState = FakeSource.OPEN;
    this.onopen?.();
  }
  send(data: string) {
    this.onmessage?.({ data });
  }
  /** the stream broke off; the browser will reconnect it */
  drop() {
    this.readyState = FakeSource.CONNECTING;
    this.onerror?.();
  }
  /** the reply was not a stream; the browser gives up on it */
  refuse() {
    this.readyState = FakeSource.CLOSED;
    this.onerror?.();
  }
  close() {
    this.closed = true;
    this.readyState = FakeSource.CLOSED;
  }
}

const last = () => FakeSource.made[FakeSource.made.length - 1];

beforeEach(() => {
  vi.useFakeTimers();
  FakeSource.made = [];
  vi.stubGlobal("EventSource", FakeSource);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("liveFeed", () => {
  it("hands over each message decoded and skips one that is not JSON", () => {
    const message = vi.fn();
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    const feed = liveFeed("/api/sse/events", { message });
    last().open();
    last().send('{"kind":"track_finalized"}');
    last().send("not json");
    expect(message).toHaveBeenCalledTimes(1);
    expect(message).toHaveBeenCalledWith({ kind: "track_finalized" });
    expect(errors).toHaveBeenCalledTimes(1);
    feed.close();
  });

  it("asks for what was missed when the feed comes back, not when it first opens", () => {
    const resync = vi.fn();
    const feed = liveFeed("/api/sse/events", { message: () => {}, resync });
    last().open();
    expect(resync).not.toHaveBeenCalled();
    last().drop();
    last().open();
    expect(resync).toHaveBeenCalledTimes(1);
    feed.close();
  });

  it("opens again after a refused reply, waiting longer each time", () => {
    const resync = vi.fn();
    const feed = liveFeed("/api/sse/identities", { message: () => {}, resync });
    last().open();
    last().refuse();
    expect(FakeSource.made).toHaveLength(1);
    vi.advanceTimersByTime(1000);
    expect(FakeSource.made).toHaveLength(2);
    last().refuse();
    vi.advanceTimersByTime(1000);
    expect(FakeSource.made).toHaveLength(2);
    vi.advanceTimersByTime(1000);
    expect(FakeSource.made).toHaveLength(3);
    last().open();
    expect(resync).toHaveBeenCalledTimes(1);
    expect(last().url).toBe("/api/sse/identities");
    feed.close();
  });

  it("says the connection is lost only once a drop outlasts a reconnect", () => {
    const feed = liveFeed("/api/sse/tracks", { message: () => {} });
    last().open();
    last().drop();
    vi.advanceTimersByTime(1000);
    expect(live.lost).toBe(false);
    vi.advanceTimersByTime(1500);
    expect(live.lost).toBe(true);
    last().open();
    expect(live.lost).toBe(false);
    feed.close();
  });

  it("stops retrying and stops counting as lost once closed", () => {
    const feed = liveFeed("/api/sse/detections", { message: () => {} });
    const first = last();
    first.refuse();
    feed.close();
    vi.advanceTimersByTime(60000);
    expect(FakeSource.made).toHaveLength(1);
    expect(first.closed).toBe(true);
    expect(live.lost).toBe(false);
  });
});
