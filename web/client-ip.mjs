// Who the api's login limiter should see as the client of a proxied request.
//
// The web port is reachable from the LAN, not only through the tunnel, so
// X-Forwarded-For is honoured only from a peer listed in BABA_TRUSTED_EDGES
// (the cloudflared host, or a reverse proxy in front of web), and only its
// rightmost hop — the one that peer appended. From anyone else it is a value
// the caller chose, and the caller itself is the client.
//
// cf-connecting-ip is deliberately not read: an edge that is not Cloudflare
// (the LAN Caddy in front of prod) passes a client's own cf-connecting-ip
// through untouched, so trusting it from every edge let any LAN host name
// itself anything. Every edge appends the real client to X-Forwarded-For.

import net from "node:net";

export function trustedEdges(spec) {
  const list = new net.BlockList();
  for (const raw of (spec || "").split(",")) {
    const entry = raw.trim();
    if (!entry) continue;
    const [addr, bits, extra] = entry.split("/");
    const family = net.isIPv4(addr) ? "ipv4" : net.isIPv6(addr) ? "ipv6" : "";
    if (!family || extra !== undefined || (bits !== undefined && !/^\d+$/.test(bits))) {
      throw new Error(`BABA_TRUSTED_EDGES: not an IP or CIDR: ${entry}`);
    }
    if (bits === undefined) list.addAddress(addr, family);
    else list.addSubnet(addr, Number(bits), family);
  }
  return list;
}

export function clientIp(peer, headers, edges) {
  const p = (peer || "").replace(/^::ffff:(?=\d+\.\d+\.\d+\.\d+$)/, "");
  if (p && edges.check(p, net.isIPv6(p) ? "ipv6" : "ipv4")) {
    const last = String(headers["x-forwarded-for"] || "").split(",").pop().trim();
    if (last) return last;
  }
  return p;
}
