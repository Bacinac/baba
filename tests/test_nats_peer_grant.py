"""The mirror account reads what a mirror needs and nothing BABA says to itself.

DIDA's adapter mirrors the per-camera snapshots, bell presses, arrivals and the
roster. BABA's services talk request/reply on the same bus; with those subjects
under `baba.state.>` and the shared `_INBOX.>` open to it, the mirror could read
a request to the state-evaluator and answer it first.
"""

import re
from pathlib import Path

from baba_core import wire

CONF = Path(__file__).resolve().parents[1] / "docker" / "nats-auth.conf"
CAMERA = "3f2a9c1e-5b7d-4e8f-9a0b-1c2d3e4f5a6b"


def _grant(kind: str) -> list[str]:
    peer = CONF.read_text().split("$BABA_PEER_NATS_USER", 1)[1]
    allow = re.search(kind + r":\s*\{\s*allow:\s*\[([^\]]*)\]", peer)
    assert allow, kind
    return re.findall(r'"([^"]+)"', allow.group(1))


def _covers(pattern: str, subject: str) -> bool:
    p, s = pattern.split("."), subject.split(".")
    for i, token in enumerate(p):
        if token == ">":
            return len(s) > i
        if i >= len(s) or token not in ("*", s[i]):
            return False
    return len(p) == len(s)


def _readable(subject: str) -> bool:
    return any(_covers(p, subject) for p in _grant("subscribe"))


def test_the_mirror_reads_every_subject_it_mirrors():
    for subject in (f"baba.state.{CAMERA}", f"baba.bell.{CAMERA}", f"baba.place.{CAMERA}",
                    "baba.roster", "_INBOX.dida.Kx8Hq2VbN7mP4rT6yW1zAc.1"):
        assert _readable(subject), subject


def test_the_mirror_neither_reads_nor_answers_what_baba_says_to_itself():
    rpc = [v for k, v in vars(wire).items() if k.startswith("SUBJECT_") and v.startswith("baba.rpc.")]
    assert len(rpc) >= 3
    service_reply = "_INBOX.Kx8Hq2VbN7mP4rT6yW1zAc.1"
    for subject in [*rpc, service_reply, f"baba.events.{CAMERA}"]:
        assert not _readable(subject), subject
    assert _grant("publish") == ["baba.roster.get"]
