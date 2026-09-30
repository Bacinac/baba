"""Every audit row the api writes is one the filter accepts and the web can name.

The vocabulary lives in `baba_api.audit`; nothing type-checks the call sites
against it, so this reads them. The filter once kept its own copy and drifted
until whole classes of rows 400'd and showed raw in the log.
"""

import ast
from pathlib import Path
from typing import get_args

from baba_api.audit import AuditOp, AuditResource

API = Path(__file__).resolve().parent.parent / "services" / "api" / "src" / "baba_api"


def _calls() -> list[tuple[str, int, dict[str, ast.expr]]]:
    found = []
    for path in sorted(API.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", getattr(node.func, "attr", None))
            if name == "write_audit":
                found.append((path.name, node.lineno, {k.arg: k.value for k in node.keywords if k.arg}))
    return found


def test_write_audit_is_called() -> None:
    assert len(_calls()) > 20


def test_every_call_uses_the_vocabulary() -> None:
    allowed = {"resource_type": set(get_args(AuditResource)), "op": set(get_args(AuditOp))}
    for where, line, kwargs in _calls():
        for arg, words in allowed.items():
            value = kwargs.get(arg)
            assert isinstance(value, ast.Constant), f"{where}:{line} passes {arg} as an expression"
            assert value.value in words, f"{where}:{line} writes {arg}={value.value!r}, not in baba_api.audit"
