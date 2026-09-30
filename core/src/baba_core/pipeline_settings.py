"""Operator-editable pipeline tuning, resolved live.

Every threshold in the pipeline used to be env-only: to change one you edited
`.env` and redeployed. That is consistent — an env value applies everywhere the
same — but it is not reachable from the application, so in practice the
thresholds that decide what BABA sees were the settings an operator could not
touch.

Two rules this module exists to enforce:

**Env is the DEFAULT, never the value.** The deployment supplies a floor; the
operator's row in `app_settings` wins when present. Clearing a field in the UI
returns it to the deployment default rather than to zero, so "reset" is always
available and always means something.

**Values are read AT USE TIME, never captured into a constructor.** That is the
whole point. A design where each object copies its thresholds at startup needs
one setter per knob to stay live, and the twenty-sixth knob is the one somebody
forgets — which is exactly how a setting ends up visible in the UI and absent
from the pipeline. Consumers hold a `Settings` and read `s.f("name")` where
they need it, so a new knob is live the moment it has a default.

Out of range is CLAMPED, not rejected: a threshold arriving from a text field
must never be able to invert a gate (a re-ID distance ≥ 1.0 would have merged
every pair with no embedding instead of rejecting it — that was a real finding,
and env had nothing guarding it either).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

__all__ = ["DEFAULTS_KEY", "Bound", "Settings", "publish_defaults"]


@dataclass(frozen=True, slots=True)
class Bound:
    """One tunable: the range it may not leave, and its label.

    Deliberately NO default. The deployment is the authority on that — this
    stack's `.env` really does set `BABA_EVENT_MIN_OBSERVATIONS=5` where the
    code default is 3, so a default baked in here would have silently retuned
    production the moment this shipped. The service passes its own env-resolved
    values in as the fallbacks.
    """

    lo: float
    hi: float
    # Shown in the UI. Kept beside the bound so the number and its explanation
    # cannot drift apart the way a help string in a template would.
    help_key: str = ""


class Settings:
    """Env defaults with an `app_settings` row layered on top.

    Owns no connection and no listener: the service already has one, and a
    second LISTEN per process is a Postgres connection spent on nothing. The
    caller re-reads the row on `app_settings_changed` and hands it here.
    """

    __slots__ = ("_bounds", "_defaults", "_values")

    def __init__(self, bounds: dict[str, Bound], defaults: dict[str, float]) -> None:
        missing = set(bounds) - set(defaults)
        if missing:
            # Fail at construction, not at the first read of a knob nobody
            # thought to wire — that would run the whole deployment on a
            # silently absent value.
            raise ValueError(f"pipeline settings: no deployment default for {sorted(missing)}")
        self._bounds = bounds
        self._defaults = {k: float(v) for k, v in defaults.items()}
        self._values: dict[str, float] = {}

    def apply(self, raw: Any) -> None:
        """Take an `app_settings.value` payload. Unknown keys are ignored, bad
        ones fall back to the default — a malformed row must not take the
        pipeline down or, worse, silently zero a gate."""
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                log.warning("pipeline settings: value is not JSON — using defaults")
                raw = None
        values: dict[str, float] = {}
        if isinstance(raw, dict):
            for name, bound in self._bounds.items():
                if name not in raw or raw[name] is None:
                    continue
                v = raw[name]
                if not isinstance(v, int | float) or isinstance(v, bool):
                    log.warning("pipeline settings: %s is not a number — ignoring", name)
                    continue
                clamped = max(bound.lo, min(bound.hi, float(v)))
                if clamped != float(v):
                    log.warning(
                        "pipeline settings: %s=%s out of range [%s, %s] — clamped to %s",
                        name,
                        v,
                        bound.lo,
                        bound.hi,
                        clamped,
                    )
                values[name] = clamped
        self._values = values

    def f(self, name: str) -> float:
        """Current value. KeyError on an unknown name — a typo here would
        otherwise read as "the operator never set it" and silently run the
        default forever."""
        self._bounds[name]  # KeyError on a typo, before it reads as "unset"
        return self._values.get(name, self._defaults[name])

    def i(self, name: str) -> int:
        return int(self.f(name))

    def is_set(self, name: str) -> bool:
        """True when the operator has a value; False when the deployment
        default is in force. Drives the "inherited" badge in the UI."""
        return name in self._values

    def bounds(self) -> dict[str, Bound]:
        return self._bounds

    def defaults(self) -> dict[str, float]:
        """What this deployment falls back to — env, as resolved at startup."""
        return dict(self._defaults)

    def as_dict(self) -> dict[str, float]:
        """Effective values, for logging and for the api to report."""
        return {name: self.f(name) for name in self._bounds}


DEFAULTS_KEY = "pipeline_defaults"


async def publish_defaults(conn, settings: Settings) -> None:
    """Record what THIS deployment's env resolved to, so the settings form can
    say what "reset" will give back.

    The api cannot work it out: the values come from each service's own
    environment, and this stack's `.env` really does differ from the code
    defaults. Merged, not replaced — the tracker and the event-manager each
    publish their own subset into the same row.

    Informational only. Nothing in the pipeline ever reads it back, so it
    cannot become a second source for a value that already has one.
    """
    import json as _json

    try:
        raw = await conn.fetchval("SELECT value FROM app_settings WHERE key = $1", DEFAULTS_KEY)
        if isinstance(raw, str):
            raw = _json.loads(raw)
        merged = dict(raw) if isinstance(raw, dict) else {}
        merged.update(settings.defaults())
        await conn.execute(
            "INSERT INTO app_settings (key, value) VALUES ($1, $2::jsonb) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            DEFAULTS_KEY,
            _json.dumps(merged),
        )
    except Exception:
        # A settings form that shows a slightly stale default is a cosmetic
        # problem; a service that will not start is not.
        log.exception("could not publish deployment defaults")
