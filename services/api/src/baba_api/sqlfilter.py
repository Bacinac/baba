"""Positional-parameter WHERE builder shared by the list endpoints.

asyncpg takes `$1, $2, …` in order, so every filtered list route has to keep a
condition list and an argument list in lockstep. That bookkeeping was copied
verbatim into six route modules, and the copies had already started to differ
in the risky direction: some built placeholders with `args.append(v)` followed
by a bare `f"${len(args)}"`, which silently points at the wrong parameter the
moment a line is inserted between the two.

`bind()` makes that pair atomic — it appends the value and hands back the
placeholder that certainly refers to it.
"""

from __future__ import annotations

from typing import Any


class SqlFilter:
    """Accumulates WHERE conditions and their positional arguments.

    Pass any always-on conditions to the constructor; they carry no
    parameters and are rendered ahead of everything added later.
    """

    __slots__ = ("args", "conds")

    def __init__(self, *always: str) -> None:
        self.conds: list[str] = list(always)
        self.args: list[Any] = []

    def bind(self, val: Any) -> str:
        """Append `val` and return the `$n` placeholder that refers to it."""
        self.args.append(val)
        return f"${len(self.args)}"

    def add(self, cond: str, val: Any) -> None:
        """Add a condition with one parameter; `?` marks where it goes.

        Every `?` in `cond` refers to the same single value, so
        `add("a = ? OR b = ?", x)` binds `x` once and reads it twice.
        """
        self.conds.append(cond.replace("?", self.bind(val)))

    def where(self, *, required: bool = False) -> str:
        """Render the clause, or `""` when nothing was added.

        `required=True` asserts at least one condition — for queries whose
        cost only stays sane while some filter is present.
        """
        if not self.conds:
            if required:
                raise ValueError("SqlFilter.where(required=True) with no conditions")
            return ""
        return "WHERE " + " AND ".join(self.conds)
