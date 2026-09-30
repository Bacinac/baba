"""URL utilities shared across BABA services.

Currently a single helper, `mask_credentials`, that strips user/password
from URLs (RTSP, HTTP, etc.) so they can be safely logged or returned in
API error responses. Ingestor and recorder previously each had their own
copy of this — kept in sync by hand, which is a footgun. Centralising it
also lets api routes (probe, discovery) use the same masking when echoing
URLs back to operators.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

# Query parameters that carry a credential. Not every camera puts its
# password in the userinfo: the doorbell's HTTP-FLV stream carries `user` and
# `password` as ordinary parameters, and until this list existed that URL went
# through the masking untouched — into the logs, and into the audit payload
# written precisely so passwords would not land there.
_SECRET_PARAMS = frozenset(
    {"password", "passwd", "pwd", "pass", "user", "username", "token",
     "auth", "secret", "key", "apikey", "api_key", "access_token"}
)
_REDACTED = "***"


def mask_credentials(url: str) -> str:
    """Return `url` with every credential it carries redacted.

    `rtsp://admin:secret@cam.lan:554/stream` → `rtsp://cam.lan:554/stream`
    `http://cam.lan/flv?stream=x&user=admin&password=s` →
    `http://cam.lan/flv?stream=x&user=***&password=***`

    The userinfo is removed and query credentials are replaced rather than
    dropped: what an operator reading a log needs to see is that the URL
    carries a password at all, and a URL with the parameter silently missing
    reads like a different URL.

    Untouched if it carries no credentials or fails to parse — better a
    slightly verbose log line than a crashed one.
    """
    try:
        p = urlparse(url)
        changed = False
        if p.username or p.password:
            netloc = p.hostname or ""
            if p.port:
                netloc = f"{netloc}:{p.port}"
            p = p._replace(netloc=netloc)
            changed = True
        if p.query:
            pairs = parse_qsl(p.query, keep_blank_values=True)
            if any(k.lower() in _SECRET_PARAMS for k, _ in pairs):
                p = p._replace(query=urlencode(
                    [(k, _REDACTED if k.lower() in _SECRET_PARAMS else v)
                     for k, v in pairs],
                    # The placeholder is for a human reading a log line, and
                    # percent-encoded asterisks read as noise.
                    quote_via=quote, safe="*",
                ))
                changed = True
        if changed:
            return urlunparse(p)
    except ValueError:
        pass
    return url
