#!/usr/bin/env python3
"""Anonymise recorded BABA fixtures for the public demo.

Two layers, both fail-closed:
  1. NAMES — real family/pet/vehicle names + plate → generic (gitignored map).
  2. SECRETS/INFRA — the recorder captures admin-level camera config, which
     carries stream URLs WITH camera passwords, RTSP URLs, private IPs and API
     keys. Those must never reach a public URL. Sensitive KEYS are replaced or
     masked and private IPs are rewritten to documentation ranges.

    python scrub_fixtures.py <in.json> <out.json>
"""
import hashlib
import json
import pathlib
import re
import sys

cfg = json.loads((pathlib.Path(__file__).parent / "anon-map.json").read_text())
NAMES, EXACT = cfg["names"], cfg["exact"]
name_pat = re.compile(r"\b(" + "|".join(sorted(map(re.escape, NAMES), key=len, reverse=True)) + r")\b")

# Keys whose VALUE is a secret or an infra locator the demo must not carry. A
# string stays a string: the screens read these as the API types them, and a
# null where a URL is promised takes the camera page down.
LOCATOR_KEYS = re.compile(r"(stream_url|substream_url|rtsp|go2rtc|webhook|dsn|host)", re.I)
LOCATOR = "rtsp://camera.example/stream"
MASK_KEYS = re.compile(r"(password|secret|token|api_?key|credential|passwd)", re.I)
IP = re.compile(r"\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b")
DOC = "203.0.113"

# Every plate the NVR read is a real car, most of them not ours: a plate is
# replaced wherever it stands (its own field, a parked car named by its plate,
# free text), the same real plate always becoming the same fake one so an
# identity and its sightings still agree. Fakes are ZG-1nnn-AB, the form
# privacy_gate.py accepts.
PLATE_KEY = re.compile(r"^plate(_text)?s?$", re.I)
PLATE_TEXT = re.compile(r"\b[A-Z]{2}[ -]?\d{3,4}[ -]?[A-Z]{1,2}\b")
FAKE_PLATE = re.compile(r"ZG-?1\d{3}-?AB")


def norm(plate):
    return re.sub(r"[^A-Z0-9]", "", plate.upper())


EXACT_PLATES = {norm(k): v for k, v in EXACT.items() if FAKE_PLATE.fullmatch(v)}


def fake_plate(real):
    if FAKE_PLATE.fullmatch(real):
        return real
    n = norm(real)
    fake = EXACT_PLATES.get(n) or f"ZG-1{int(hashlib.sha256(n.encode()).hexdigest(), 16) % 1000:03d}-AB"
    return fake if "-" in real or " " in real else fake.replace("-", "")


def looks_like_plate(v):
    return isinstance(v, str) and re.fullmatch(r"[A-Z0-9][A-Z0-9 -]{2,10}", v) and re.search(r"\d", v)


def fake_ip(m):
    return f"{DOC}.{int(m.group(0).rsplit('.', 1)[1]) % 254 + 1}"


def scrub_str(s):
    for a, b in EXACT.items():
        s = s.replace(a, b)
    s = name_pat.sub(lambda m: NAMES[m.group(1)], s)
    s = re.sub(r"rtsp://[^\s\"']+", "rtsp://camera.example/stream", s)
    s = re.sub(r"https?://[^\s\"']*(user|password|token)=[^\s\"']+", "http://camera.example/stream", s)
    s = IP.sub(fake_ip, s)
    s = PLATE_TEXT.sub(lambda m: fake_plate(m.group(0)), s)
    return s


def walk(x):
    if isinstance(x, str):
        return scrub_str(x)
    if isinstance(x, list):
        return [walk(v) for v in x]
    if isinstance(x, dict):
        out = {}
        parked = "place" in x
        for k, v in x.items():
            if isinstance(k, str) and PLATE_KEY.match(k):
                out[k] = [fake_plate(p) for p in v] if isinstance(v, list) else fake_plate(v) if isinstance(v, str) else v
            elif parked and k == "name" and looks_like_plate(v):
                out[k] = fake_plate(v)
            elif isinstance(k, str) and LOCATOR_KEYS.search(k):
                out[k] = LOCATOR if isinstance(v, str) else None
            elif isinstance(k, str) and MASK_KEYS.search(k):
                out[k] = "***" if v else v
            else:
                out[k] = walk(v)
        return out
    return x


data = json.loads(pathlib.Path(sys.argv[1]).read_text())
pathlib.Path(sys.argv[2]).write_text(json.dumps(walk(data), ensure_ascii=False))
print("scrubbed ->", sys.argv[2])
