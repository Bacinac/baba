"""Minimal ONVIF client. Hand-rolled SOAP over httpx — no zeep, no lxml.

We need only three calls to enrich camera discovery:
  • GetDeviceInformation — vendor / model
  • GetProfiles          — list of stream profile tokens
  • GetStreamUri         — RTSP URL for a given profile

Authentication uses WS-Security UsernameToken with PasswordDigest:
    digest = Base64( SHA1( nonce_raw + created_iso + password ) )

We try a few common entry points (port 80 + 8000) because vendor habits
differ. The first endpoint that responds with a valid SOAP envelope wins.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

log = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class OnvifDeviceInfo:
    vendor: str | None
    model: str | None


@dataclass(slots=True, frozen=True)
class OnvifStream:
    profile_token: str
    profile_name: str
    rtsp_uri: str


def _ws_security_header(username: str, password: str) -> str:
    nonce_raw = os.urandom(16)
    nonce_b64 = base64.b64encode(nonce_raw).decode()
    created = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = base64.b64encode(
        hashlib.sha1(nonce_raw + created.encode() + password.encode()).digest()
    ).decode()
    return (
        "<s:Header>"
        '<Security xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd" '
        's:mustUnderstand="1">'
        "<UsernameToken>"
        f"<Username>{username}</Username>"
        '<Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">'
        f"{digest}</Password>"
        '<Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">'
        f"{nonce_b64}</Nonce>"
        f'<Created xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">{created}</Created>'
        "</UsernameToken>"
        "</Security>"
        "</s:Header>"
    )


def _envelope(body: str, *, header: str = "") -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
        f"{header}"
        f"<s:Body>{body}</s:Body>"
        "</s:Envelope>"
    ).encode()


# Quick-and-dirty XML extractor — we don't need full schema parsing for the
# fields we want, just the text content of named elements. Works across the
# namespace prefix variations ONVIF cameras use in practice.
def _find(xml: str, name: str) -> str | None:
    m = re.search(rf"<(?:\w+:)?{re.escape(name)}[^>]*>([^<]+)</(?:\w+:)?{re.escape(name)}>", xml)
    return m.group(1).strip() if m else None



class OnvifClient:
    """Async ONVIF client. Probes the common service endpoints and remembers
    the first that answers. Use as: `async with OnvifClient(ip, user, pass) as c: ...`"""

    # Most vendors expose the device service on :80; the rest scatter it across
    # a handful of vendor-specific ports. TP-Link (Tapo/VIGI) uses :2020 —
    # missing it meant ONVIF silently failed on those cams and we fell back to
    # blind RTSP-path guessing (verified on a Tapo C310: ONVIF only on :2020,
    # GetStreamUri → /stream1 + /stream2). Ordered most-common first; probing
    # stops at the first endpoint that answers.
    CANDIDATE_ENDPOINTS: tuple[str, ...] = (
        "http://{ip}/onvif/device_service",
        "http://{ip}:2020/onvif/device_service",  # TP-Link Tapo / VIGI
        "http://{ip}:8000/onvif/device_service",  # Reolink, older Hikvision sub-models
        "http://{ip}:8080/onvif/device_service",
        "http://{ip}:8899/onvif/device_service",  # XM / Sofia generic OEMs
    )

    def __init__(self, ip: str, username: str, password: str, *, timeout_s: float = 4.0):
        self.ip = ip
        self.username = username
        self.password = password
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout_s))
        self._device_url: str | None = None  # discovered

    async def __aenter__(self) -> OnvifClient:
        return self

    async def __aexit__(self, *_exc) -> None:
        await self._client.aclose()

    async def _post(self, url: str, body: str, *, authed: bool) -> str | None:
        header = _ws_security_header(self.username, self.password) if authed else ""
        try:
            r = await self._client.post(
                url,
                content=_envelope(body, header=header),
                headers={"Content-Type": "application/soap+xml; charset=utf-8"},
            )
        except httpx.HTTPError as e:
            log.debug("onvif post %s failed: %s", url, e)
            return None
        if r.status_code >= 500:
            # Auth fault still returns 200 with SOAP fault; 5xx means real error.
            return None
        return r.text

    async def _ensure_endpoint(self) -> str | None:
        if self._device_url is not None:
            return self._device_url
        # Probe with unauthenticated GetCapabilities — every ONVIF device
        # accepts this without auth; the body tells us nothing useful but the
        # response confirms the endpoint exists.
        probe_body = (
            '<GetCapabilities xmlns="http://www.onvif.org/ver10/device/wsdl">'
            "<Category>All</Category></GetCapabilities>"
        )
        for tpl in self.CANDIDATE_ENDPOINTS:
            url = tpl.format(ip=self.ip)
            xml = await self._post(url, probe_body, authed=False)
            if xml and ("onvif.org" in xml.lower() or "envelope" in xml.lower()):
                self._device_url = url
                return url
        return None

    async def get_device_information(self) -> OnvifDeviceInfo | None:
        url = await self._ensure_endpoint()
        if url is None:
            return None
        body = '<GetDeviceInformation xmlns="http://www.onvif.org/ver10/device/wsdl"/>'
        xml = await self._post(url, body, authed=True)
        if xml is None or "fault" in xml.lower():
            return None
        return OnvifDeviceInfo(
            vendor=_find(xml, "Manufacturer"),
            model=_find(xml, "Model"),
        )

    async def get_streams(self) -> list[OnvifStream]:
        """Return RTSP URIs for every profile the device advertises.
        Newer firmware returns multiple (main / sub / mobile); we surface them
        all so the UI can show the user which one to pick."""
        url = await self._ensure_endpoint()
        if url is None:
            return []

        # GetProfiles → list of <Profiles token="...">…<Name>…</Name>…</Profiles>
        body = '<GetProfiles xmlns="http://www.onvif.org/ver10/media/wsdl"/>'
        xml = await self._post(url, body, authed=True)
        if xml is None:
            return []

        # Pull each Profiles block separately so we don't mix up tokens/names.
        blocks = re.findall(
            r"<(?:\w+:)?Profiles\b[^>]*token=\"([^\"]+)\"[^>]*>(.*?)</(?:\w+:)?Profiles>",
            xml,
            flags=re.DOTALL,
        )
        if not blocks:
            return []

        out: list[OnvifStream] = []
        for token, inner in blocks:
            name = _find(inner, "Name") or token
            uri_body = (
                '<GetStreamUri xmlns="http://www.onvif.org/ver10/media/wsdl">'
                "<StreamSetup>"
                '<Stream xmlns="http://www.onvif.org/ver10/schema">RTP-Unicast</Stream>'
                '<Transport xmlns="http://www.onvif.org/ver10/schema"><Protocol>RTSP</Protocol></Transport>'
                "</StreamSetup>"
                f"<ProfileToken>{token}</ProfileToken>"
                "</GetStreamUri>"
            )
            uri_xml = await self._post(url, uri_body, authed=True)
            if uri_xml is None:
                continue
            uri = _find(uri_xml, "Uri")
            if uri:
                out.append(OnvifStream(profile_token=token, profile_name=name, rtsp_uri=uri))
        return out
