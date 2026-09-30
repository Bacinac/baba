"""pgvector <-> numpy bridging.

asyncpg has no native pgvector codec, so every service that touches an
embedding column serialises to the textual `[x1,x2,...]` literal on write
and parses the same text form on read. These two helpers used to be
copy-pasted in the embedder, state-evaluator, api identities routes and
visual search — one drifting copy per service. Single source of truth here.
"""

from __future__ import annotations

from typing import Any

import numpy as np


def vector_literal(v: np.ndarray) -> str:
    """numpy vector → pgvector textual literal, e.g. `[0.1,0.2,...]`.
    Cast with `::vector` on the SQL side."""
    return "[" + ",".join(f"{float(x):.6f}" for x in v.tolist()) + "]"


def parse_vector(v: Any) -> np.ndarray:
    """pgvector text form (`[0.1,0.2,...]`, as asyncpg returns the column)
    → float32 ndarray. Empty input yields a zero-length vector."""
    s = v if isinstance(v, str) else str(v)
    inner = s.strip().lstrip("[").rstrip("]")
    if not inner:
        return np.zeros((0,), dtype=np.float32)
    return np.array(inner.split(","), dtype=np.float32)
