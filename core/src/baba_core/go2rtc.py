"""go2rtc's API credentials, from the standard BABA environment.

go2rtc runs network_mode:host, so its REST API listens on every interface —
and that API answers /api/config with each camera's full RTSP URL, passwords in
clear text. Anything on the LAN that can reach the port owns the cameras. BABA
therefore gates it with basic auth, and every internal client (api's stream
sync + snapshot proxy, the web proxy, event-manager's thumbnail capture, the
container healthcheck) must present the same pair.

That "every client" is why this lives in core rather than in one service: the
first copy of the helper sat in baba_api.config, and event-manager — which
fetches live thumbnails straight from go2rtc — could not see it. Turning auth
on would have quietly stopped thumbnails while everything else kept working.

There is no open mode. An empty BABA_GO2RTC_API_PASSWORD is a broken install,
and a client that started anyway would leave go2rtc open to the whole LAN.
"""

from __future__ import annotations

import os


def go2rtc_auth_from_env() -> tuple[str, str]:
    """Basic-auth (user, password) for go2rtc's API."""
    password = os.environ.get("BABA_GO2RTC_API_PASSWORD", "")
    if not password:
        raise RuntimeError("BABA_GO2RTC_API_PASSWORD is required — run ./install.sh --upgrade")
    user = os.environ.get("BABA_GO2RTC_API_USER", "").strip() or "baba"
    return (user, password)
