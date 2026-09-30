"""LAN camera discovery.

Two-phase probe:

  1. TCP fan-out: for each host in the subnet, check whether camera-typical
     ports respond. /24 with 100 parallel workers settles in seconds.
  2. Protocol probe: for the small subset of (ip, port) pairs that opened,
     send a real protocol message (RTSP OPTIONS, ONVIF SOAP) and require a
     response that looks like a camera. This filters out routers, NAS units,
     printers and other devices that happen to listen on port 80 or 8080.

Only IPs that pass at least one protocol-level probe are returned. We
attach `signals` (e.g. ["rtsp", "onvif"]) so the UI can show why we
consider it a camera.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

discovery_router = APIRouter()


# Phase-1 ports. RTSP gets its own treatment; the rest are HTTP-ish and get
# the ONVIF SOAP probe.
_RTSP_PORTS: tuple[int, ...] = (554, 8554)
_HTTP_PORTS: tuple[int, ...] = (80, 2020, 8000, 8080, 8081, 8899)  # 2020 = TP-Link Tapo/VIGI ONVIF
_PROBE_PORTS: tuple[int, ...] = _RTSP_PORTS + _HTTP_PORTS

_CONNECT_TIMEOUT = 0.8  # phase 1: TCP handshake only
_PROBE_TIMEOUT = 1.5  # phase 2: protocol talk
_MAX_CONCURRENCY = 200


class DiscoverIn(BaseModel):
    subnet: str = Field(..., description="CIDR notation, e.g. 192.168.1.0/24")


class DiscoveredCamera(BaseModel):
    ip: str
    open_ports: list[int]
    signals: list[str]  # e.g. ["rtsp:554", "onvif:80"]


class DiscoverOut(BaseModel):
    duration_ms: int
    scanned: int
    found: list[DiscoveredCamera]


async def _tcp_open(ip: str, port: int, sem: asyncio.Semaphore) -> bool:
    async with sem:
        loop = asyncio.get_running_loop()
        try:
            conn = loop.create_connection(lambda: asyncio.Protocol(), ip, port)
            transport, _ = await asyncio.wait_for(conn, timeout=_CONNECT_TIMEOUT)
        except (TimeoutError, OSError):
            return False
        transport.close()
        return True


async def _exchange(ip: str, port: int, request: bytes, read_bytes: int) -> bytes | None:
    """Send one request and return the start of the reply, or None when the
    port does not talk back in time."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port),
            timeout=_PROBE_TIMEOUT,
        )
    except (TimeoutError, OSError):
        return None
    try:
        writer.write(request)
        await writer.drain()
        return await asyncio.wait_for(reader.read(read_bytes), timeout=_PROBE_TIMEOUT)
    except (TimeoutError, OSError):
        return None
    finally:
        with contextlib.suppress(OSError):
            writer.close()
            await writer.wait_closed()


async def _rtsp_probe(ip: str, port: int) -> bool:
    """Send a minimal RTSP OPTIONS and accept any 200/401 reply. 401 is fine
    — it means there's an RTSP server, just guarded by auth."""
    req = (
        f"OPTIONS rtsp://{ip}:{port}/ RTSP/1.0\r\nCSeq: 1\r\nUser-Agent: BABA-Discovery\r\n\r\n"
    ).encode()
    data = await _exchange(ip, port, req, 256)
    if data is None:
        return False
    head = data[:20].decode(errors="replace").upper()
    return head.startswith("RTSP/1.0 200") or head.startswith("RTSP/1.0 401")


_ONVIF_SOAP = (
    b"<?xml version='1.0' encoding='UTF-8'?>"
    b"<s:Envelope xmlns:s='http://www.w3.org/2003/05/soap-envelope'>"
    b"<s:Body>"
    b"<GetCapabilities xmlns='http://www.onvif.org/ver10/device/wsdl'>"
    b"<Category>All</Category>"
    b"</GetCapabilities>"
    b"</s:Body>"
    b"</s:Envelope>"
)


async def _onvif_probe(ip: str, port: int) -> bool:
    """POST a SOAP GetCapabilities to /onvif/device_service. Real ONVIF
    devices respond with a SOAP envelope (success OR auth fault); generic
    web servers 404 / 405 / serve HTML. We accept any response whose body
    contains an ONVIF/SOAP namespace."""
    req = (
        f"POST /onvif/device_service HTTP/1.1\r\n"
        f"Host: {ip}:{port}\r\n"
        f"Content-Type: application/soap+xml; charset=utf-8\r\n"
        f"Content-Length: {len(_ONVIF_SOAP)}\r\n"
        f"User-Agent: BABA-Discovery\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    ).encode() + _ONVIF_SOAP
    # Read enough to see headers + first chunk of body. ONVIF replies
    # are typically a few KB; first 2 KB has the namespace if any.
    data = await _exchange(ip, port, req, 2048)
    if data is None:
        return False
    lower = data.lower()
    # Either successful SOAP, or even a SOAP Fault for auth — both prove
    # the endpoint speaks ONVIF.
    return b"onvif.org" in lower or (b"soap" in lower and b"envelope" in lower)


async def _classify(ip: str, open_ports: list[int]) -> tuple[str, list[str]] | None:
    """Run protocol probes against the open ports. Returns (ip, signals) if
    at least one probe says "camera"; otherwise None."""
    probes = [(f"rtsp:{p}", _rtsp_probe(ip, p)) for p in open_ports if p in _RTSP_PORTS]
    probes += [(f"onvif:{p}", _onvif_probe(ip, p)) for p in open_ports if p in _HTTP_PORTS]
    answers = await asyncio.gather(*(probe for _, probe in probes))
    signals = [label for (label, _), ok in zip(probes, answers, strict=True) if ok]
    if not signals:
        return None
    return ip, signals


@discovery_router.post("/cameras/discover", response_model=DiscoverOut)
async def discover(payload: DiscoverIn) -> DiscoverOut:
    try:
        network = ipaddress.ip_network(payload.subnet, strict=False)
    except ValueError as e:
        raise HTTPException(400, f"invalid subnet: {e}") from e

    if network.num_addresses > 1024:
        raise HTTPException(400, "subnet too large; use /22 or smaller")

    hosts = list(network.hosts()) if isinstance(network, ipaddress.IPv4Network) else list(network)
    sem = asyncio.Semaphore(_MAX_CONCURRENCY)
    start = time.monotonic()

    # Phase 1: TCP fan-out.
    matrix = [(str(h), p) for h in hosts for p in _PROBE_PORTS]
    results = await asyncio.gather(*(_tcp_open(ip, p, sem) for ip, p in matrix))
    open_by_ip: dict[str, list[int]] = {}
    for (ip, p), ok in zip(matrix, results, strict=False):
        if ok:
            open_by_ip.setdefault(ip, []).append(p)

    # Phase 2: protocol probes against the (now small) set of responsive hosts.
    classifications = await asyncio.gather(
        *(_classify(ip, sorted(set(ports))) for ip, ports in open_by_ip.items())
    )
    found_signals: dict[str, list[str]] = {}
    for c in classifications:
        if c is None:
            continue
        ip, sigs = c
        found_signals[ip] = sigs

    devices = [
        DiscoveredCamera(
            ip=ip,
            open_ports=sorted(set(open_by_ip[ip])),
            signals=found_signals[ip],
        )
        for ip in sorted(found_signals, key=lambda s: tuple(int(p) for p in s.split(".")))
    ]
    return DiscoverOut(
        duration_ms=int((time.monotonic() - start) * 1000),
        scanned=len(hosts),
        found=devices,
    )
