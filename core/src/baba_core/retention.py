"""How long a track is kept: the longest tier its identity qualifies for.

90 days once the operator has enrolled reference photos for the identity, a
commitment to keeping that subject; 60 once it is only named; 30 for everything
else, hidden tracks included. Finalize sets a track's deadline, and labelling,
enrolment and every late link raise it — with GREATEST, never shortening one.
"""

from __future__ import annotations

from datetime import timedelta

__all__ = ["ANONYMOUS", "ENROLLED", "LABELLED", "tier_sql"]

ENROLLED = timedelta(days=90)
LABELLED = timedelta(days=60)
ANONYMOUS = timedelta(days=30)


def _interval(tier: timedelta) -> str:
    return f"interval '{tier.days} days'"


def tier_sql(global_id: str) -> str:
    """The tier as an SQL interval, for the identity the SQL expression
    `global_id` evaluates to."""
    return f"""CASE
        WHEN EXISTS (
            SELECT 1 FROM identity_reference_photos WHERE global_id = {global_id}
        ) THEN {_interval(ENROLLED)}
        WHEN EXISTS (
            SELECT 1 FROM identity_labels WHERE global_id = {global_id}
        ) THEN {_interval(LABELLED)}
        ELSE {_interval(ANONYMOUS)}
    END"""  # noqa: S608
