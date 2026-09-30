"""The login limits, keyed on a client address the caller cannot write.

X-Forwarded-For counts only from the peers named in BABA_TRUSTED_PROXIES (the
web proxy in front). Trust is per peer, not a global switch: the api port stays
published on the LAN (DIDA reads it there), so a header honoured from anyone
would let any LAN host rotate fake addresses past the login limit.
"""

from __future__ import annotations

import os

from home_core.rate_limit import LoginLimits, TrustedPeers

TRUSTED_PROXIES = TrustedPeers(os.environ.get("BABA_TRUSTED_PROXIES", ""))

LOGIN = LoginLimits(TRUSTED_PROXIES)
