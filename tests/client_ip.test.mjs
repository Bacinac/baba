// The web port is reachable from the LAN, not only through the tunnel, so the
// client address a request claims to forward counts only from a trusted edge,
// and only as the X-Forwarded-For hop that edge appended.

import assert from "node:assert/strict";
import { test } from "node:test";
import { clientIp, trustedEdges } from "../web/client-ip.mjs";

const EDGES = trustedEdges("192.168.1.101");

test("a LAN caller is its own client, whatever header it forges", () => {
  const h = { "cf-connecting-ip": "1.2.3.4", "x-forwarded-for": "5.6.7.8" };
  assert.equal(clientIp("192.168.10.50", h, EDGES), "192.168.10.50");
});

test("an edge names the client by the hop it appended", () => {
  const h = { "x-forwarded-for": "6.6.6.6, 203.0.113.9" };
  assert.equal(clientIp("192.168.1.101", h, EDGES), "203.0.113.9");
});

test("cf-connecting-ip counts for nothing, even from an edge", () => {
  // The LAN Caddy in front of prod passes a client's own cf-connecting-ip
  // through untouched.
  const h = { "cf-connecting-ip": "6.6.6.6", "x-forwarded-for": "6.6.6.6, 192.168.1.210" };
  assert.equal(clientIp("192.168.1.101", h, EDGES), "192.168.1.210");
});

test("an edge that forwards nothing is the client", () => {
  assert.equal(clientIp("192.168.1.101", {}, EDGES), "192.168.1.101");
});

test("nothing is trusted by default", () => {
  const h = { "x-forwarded-for": "203.0.113.9" };
  assert.equal(clientIp("192.168.1.101", h, trustedEdges(undefined)), "192.168.1.101");
});

test("IPv4-mapped peers and CIDRs", () => {
  const lan = trustedEdges("192.168.1.0/24");
  const h = { "x-forwarded-for": "203.0.113.9" };
  assert.equal(clientIp("::ffff:192.168.1.101", h, lan), "203.0.113.9");
  assert.equal(clientIp("::ffff:192.168.2.1", h, lan), "192.168.2.1");
});

test("a malformed entry fails loudly instead of trusting nobody silently", () => {
  for (const bad of ["cloudflared", "192.168.1.0/x", "10.0.0.0/8/9"]) {
    assert.throws(() => trustedEdges(bad), /BABA_TRUSTED_EDGES/);
  }
});
