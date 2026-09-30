"""Validation for user-supplied camera stream URLs before they reach a
subprocess (ffprobe / ffmpeg) or get persisted.

ffprobe/ffmpeg default to a broad protocol set that includes `file`, `concat`
and `subfile`. Handing them an unvalidated caller URL turns the subprocess into
an SSRF / local-file-read primitive: `file:///etc/passwd` leaks file metadata
back through ffprobe's output, and an `http` input that 302-redirects to
`file://` does the same. Internal-IP reachability is inherent to LAN cameras and
can't be fully removed, but the file-read and playlist vectors close cleanly by
(1) rejecting non-stream URL schemes at the door and (2) constraining the
subprocess to a stream-only `-protocol_whitelist`.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import HTTPException

# Schemes a real camera stream uses. Everything else — file, concat, subfile,
# data, gopher, … — is rejected.
_ALLOWED_SCHEMES = frozenset({"rtsp", "rtsps", "http", "https", "rtmp", "rtmps"})

# Passed as ffprobe/ffmpeg `-protocol_whitelist` so even a validated http/rtsp
# URL can't be redirected into a file/concat/subfile read. Carries every
# transport the allowed schemes actually need (tcp/udp/tls/crypto/rtp/data) but
# deliberately omits file/concat/subfile/pipe.
FFMPEG_PROTOCOL_WHITELIST = "rtsp,rtsps,rtp,rtmp,rtmps,http,https,tcp,udp,tls,crypto,data"


def validate_stream_url(url: str) -> str:
    """Return `url` unchanged if its scheme is an allowed stream scheme, else
    raise HTTPException(400). Call before spawning ffprobe/ffmpeg on it or
    persisting it to the cameras table."""
    scheme = urlsplit(url).scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise HTTPException(
            400,
            f"unsupported stream URL scheme {scheme!r} — allowed: "
            f"{', '.join(sorted(_ALLOWED_SCHEMES))}",
        )
    return url
