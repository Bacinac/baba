"""URL shapes of the cameras we know, for the camera form and for probing.

A preset is one vendor's pair of templates: the camera's own stream and, where
the vendor has one, its low-resolution substream. Placeholders are {ip},
{user}, {pass} and, for a free-form RTSP path, {path}. Ordered specific →
generic, because the form reads a stored URL back as the first preset that
describes it. The list itself is stream_presets.json, which the web tests read
as well.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote


@dataclass(frozen=True, slots=True)
class StreamPreset:
    id: str
    label: str
    main: str
    sub: str | None = None


STREAM_PRESETS: tuple[StreamPreset, ...] = tuple(
    StreamPreset(**p) for p in json.loads(Path(__file__).with_name("stream_presets.json").read_text())
)


def fill(template: str, *, ip: str, user: str, password: str, path: str = "") -> str:
    """Credentials go in percent-encoded, so a password with @ : / ? # &
    survives in RTSP userinfo and in the FLV query string alike."""
    return (
        template.replace("{user}", quote(user, safe=""))
        .replace("{pass}", quote(password, safe=""))
        .replace("{ip}", ip.strip())
        .replace("{path}", path.strip().lstrip("/"))
    )
