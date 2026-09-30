// The camera form reads a stored row back into the preset, fields and stream
// choice it was built from, and saving it untouched must store the same row —
// for every preset the api serves, with a password that needs encoding.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { recogniseStreams, streamsFor } from "../web/src/lib/streamPresets.ts";

const PRESETS = JSON.parse(
  readFileSync(new URL("../services/api/src/baba_api/stream_presets.json", import.meta.url), "utf8"),
);
const PARTS = { ip: "192.168.10.12", user: "admin", pass: "p@ss:w/rd&?#%", path: "" };

test("every preset and choice reads back into the row it stored", () => {
  for (const p of PRESETS) {
    const free = p.main.includes("{path}");
    for (const choice of p.sub ? ["main", "sub", "both"] : ["main"]) {
      const stored = streamsFor(p, { ...PARTS, path: free ? "live/ch0" : "" }, free ? "live/ch1" : "", choice, "sub");
      const r = recogniseStreams(PRESETS, stored.stream_url, stored.substream_url);
      assert.ok(r, `${p.id}/${choice} not recognised`);
      assert.equal(r.presetId, p.id, `${p.id}/${choice} read as ${r.presetId}`);
      assert.equal(r.parts.pass, PARTS.pass);
      const again = streamsFor(PRESETS.find(x => x.id === r.presetId), r.parts, r.subPath, r.choice, stored.analysis_stream);
      assert.deepEqual(again, stored, `${p.id}/${choice}`);
    }
  }
});

test("only a camera that exposes both streams chooses which one is analysed", () => {
  const reolink = PRESETS.find(p => p.id === "reolink_flv");
  assert.equal(streamsFor(reolink, PARTS, "", "both", "sub").analysis_stream, "sub");
  const subOnly = streamsFor(reolink, PARTS, "", "sub", "sub");
  assert.equal(subOnly.substream_url, null);
  assert.match(subOnly.stream_url, /channel0_sub\.bcs/);
  assert.equal(subOnly.analysis_stream, "main", "its one stream is the camera's name in go2rtc");
});

test("a preset without a substream exposes its main stream whatever was picked", () => {
  const axis = PRESETS.find(p => p.id === "axis_rtsp");
  assert.equal(axis.sub, null);
  assert.deepEqual(streamsFor(axis, PARTS, "", "both", "sub").substream_url, null);
});

test("a URL no preset describes is left to the raw fields", () => {
  assert.equal(recogniseStreams(PRESETS, "rtsp://192.168.10.5/live", null), null);
  assert.equal(recogniseStreams(PRESETS, "rtsp://a:b@10.0.0.1:554/Preview_01_main", "rtsp://c:d@10.0.0.1:554/Preview_01_sub"), null,
    "two streams with different credentials are not one camera's pair");
});
