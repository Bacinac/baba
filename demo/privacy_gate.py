#!/usr/bin/env python3
"""Refuse to ship demo fixtures that still carry a secret, an infra locator, or a
real name. Fail-CLOSED: exits non-zero on any hit so the build stops and nothing
reaches a public URL. Backstop for scrub_fixtures.py — a leak here is unfixable
once cached publicly (learned the hard way 2026-07-24: camera stream URLs carry
the admin password in clear text).

    python privacy_gate.py <fixtures.json>
"""
import contextlib
import json
import pathlib
import re
import sys

cfg = json.loads((pathlib.Path(__file__).parent / "anon-fixtures.json").read_text())
REAL_NAMES = list(cfg["names"]) + [k for k in cfg["exact"] if not re.search(r"\d", k)]
# Real literals (real plate, real possessive forms) that must NOT survive — a
# FAKE plate on a demo vehicle identity is legitimate, only the real one is not.
REAL_LITERALS = list(cfg["exact"])
NAME_RE = re.compile(r"\b(" + "|".join(map(re.escape, REAL_NAMES)) + r")\b", re.I | re.U)

VALUE_PATTERNS = {
    "rtsp URL":        re.compile(r"rtsp://[^\s\"']+", re.I),
    "credential URL":  re.compile(r"https?://[^\s\"']*(?:user|password|token|passwd)=[^\s\"']+", re.I),
    "private IP":      re.compile(r"\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b"),
    "MAC":             re.compile(r"\b(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b"),
    "inline password": re.compile(r"(?:password|passwd|token|api[_-]?key|secret)=\S+", re.I),
}
SECRET_KEY = re.compile(r"(password|passwd|secret|token|api[_-]?key|credential|stream_url|substream_url|rtsp|dsn)", re.I)
# The scrubber keeps a locator a string (the screens need one) by writing this
# bare URL on the reserved .example domain; with no userinfo, query or other host
# it carries nothing, so it is the one locator value that passes.
# A plate is a real car unless it is one of scrub_fixtures.py's fakes.
PLATE_KEY = re.compile(r"^plate(_text)?s?$", re.I)
PLATE_TEXT = re.compile(r"\b[A-Z]{2}[ -]?\d{3,4}[ -]?[A-Z]{1,2}\b")
FAKE_PLATE = re.compile(r"ZG-?1\d{3}-?AB")
PLACEHOLDER = re.compile(r"(?:rtsp|https?)://camera\.example/stream(?![^\s\"'])")

hits = []


def flat(text):
    if "\\u" in text:
        with contextlib.suppress(Exception):
            text = text.encode("utf-8", "surrogatepass").decode("unicode_escape")
    return text


def check_value(where, v):
    t = PLACEHOLDER.sub("", flat(str(v)))
    for label, pat in VALUE_PATTERNS.items():
        for m in pat.findall(t):
            hits.append(f"{where}: {label} -> {(''.join(m) if isinstance(m, tuple) else m)[:60]!r}")
    for m in PLATE_TEXT.findall(t):
        if not FAKE_PLATE.fullmatch(m):
            hits.append(f"{where}: plate -> {m!r}")
    for m in NAME_RE.findall(t):
        hits.append(f"{where}: real name -> {m!r}")
    for lit in REAL_LITERALS:
        if lit in t:
            hits.append(f"{where}: real value -> {lit!r}")


def walk(node, where):
    if isinstance(node, dict):
        for k, v in node.items():
            plates = v if isinstance(v, list) else [v]
            if isinstance(k, str) and PLATE_KEY.match(k):
                for p in plates:
                    if isinstance(p, str) and p and not FAKE_PLATE.fullmatch(p):
                        hits.append(f"{where}.{k}: real plate -> {p!r}")
            if (k == "name" and "place" in node and isinstance(v, str) and re.search(r"\d", v)
                    and not FAKE_PLATE.fullmatch(v)):
                hits.append(f"{where}.{k}: parked car named by a real plate -> {v!r}")
            # a sensitive key must carry no real value (null / masked only)
            if (isinstance(k, str) and SECRET_KEY.search(k) and v not in (None, "", "***")
                    and not (isinstance(v, str) and PLACEHOLDER.fullmatch(v))):
                hits.append(f"{where}.{k}: sensitive key still populated -> {str(v)[:50]!r}")
            walk(v, f"{where}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            walk(v, f"{where}[{i}]")
    elif isinstance(node, str):
        check_value(where, node)
        s = node.strip()
        if s.startswith(("{", "[")):
            with contextlib.suppress(ValueError):
                walk(json.loads(s), where)


def main():
    data = json.loads(pathlib.Path(sys.argv[1]).read_text())
    for key, value in data.items():
        walk(value, key)
    if hits:
        print("REFUSING: sensitive data in fixtures:", file=sys.stderr)
        for h in sorted(set(hits))[:40]:
            print("  -", h, file=sys.stderr)
        return 1
    print(f"clean - {len(data)} endpoints, no secrets/IPs/names")
    return 0


if __name__ == "__main__":
    sys.exit(main())
