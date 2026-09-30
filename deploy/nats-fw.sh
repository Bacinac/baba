#!/usr/bin/env bash
# Once .env publishes the bus beyond loopback (BABA_NATS_BIND_IP), only BABA's
# own containers and the hosts in BABA_NATS_PEER_IPS reach its ports. The
# accounts guard the bus either way, but the unrestricted service account
# answers on 4222 to anyone who can reach the port; published, that would be the
# whole LAN.
#
# Owns every DOCKER-USER rule for the bus ports, so rules written by hand before
# this script existed are replaced, not stacked on. Run by each deploy and, via
# baba-nats-fw.service, at boot — iptables rules do not survive a reboot.
set -euo pipefail

cd "$(dirname "$0")/.."
env_read() { [ -f .env ] || return 0; sed -n "s/^$1=//p" .env | tail -1; }

chain=BABA-NATS
ports=4222,4223

iptables -S DOCKER-USER | { grep -E -- "--dports? (4222|4223|$ports)( |$)" || true; } | sed 's/^-A /-D /' \
    | while read -r -a rule; do iptables "${rule[@]}"; done
iptables -F "$chain" 2>/dev/null && iptables -X "$chain"

bind=$(env_read BABA_NATS_BIND_IP)
ws_bind=$(env_read BABA_NATS_WS_BIND_IP)
published=0
for ip in "$bind" "$ws_bind"; do
    case "$ip" in ""|127.*) ;; *) published=1 ;; esac
done
[ "$published" -eq 1 ] || exit 0

peers=$(env_read BABA_NATS_PEER_IPS | tr ',' ' ')
if [ -z "${peers// /}" ]; then
    echo "nats-fw: .env publishes the bus but BABA_NATS_PEER_IPS names no host allowed to reach it" >&2
    exit 1
fi
subnets=$(docker network inspect -f '{{range .IPAM.Config}}{{.Subnet}} {{end}}' baba_default)

iptables -N "$chain"
for src in $subnets $peers; do
    iptables -A "$chain" -s "$src" -j RETURN
done
iptables -A "$chain" -j DROP
iptables -I DOCKER-USER -p tcp -m multiport --dports "$ports" -j "$chain"
echo "nats-fw: bus ports $ports reachable from $subnets and $peers only"
