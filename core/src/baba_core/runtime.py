"""Shared service entrypoint: one way every BABA service runs its main coroutine.

`uvloop.run()` rather than `uvloop.install()` + `asyncio.run()`: the install
path sets an event-loop policy, an API Python 3.14 deprecates.

uvloop 0.22.1 (the latest release) still calls the deprecated
`asyncio.iscoroutinefunction` inside its compiled `add_signal_handler` on
Python 3.14. The fix is merged upstream (MagicStack/uvloop#705) but unreleased
and the Cython can't be patched here, so that one upstream DeprecationWarning is
filtered. Remove the filter with the uvloop release that carries the fix.
"""

from __future__ import annotations

import warnings
from collections.abc import Coroutine
from typing import Any

import uvloop


def run_service(main: Coroutine[Any, Any, Any]) -> None:
    """Run a BABA service coroutine on uvloop until it returns."""
    warnings.filterwarnings(
        "ignore",
        message=r"'asyncio\.iscoroutinefunction' is deprecated",
        category=DeprecationWarning,
    )
    uvloop.run(main)
