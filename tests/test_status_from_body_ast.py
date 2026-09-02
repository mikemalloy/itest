"""F14 regression: classify a test body by AST, not string-matching.

`_status_from_body` found ``def name(`` as a substring and read to the next
top-level ``\\ndef ``. That misreads a class-nested method, and picks the wrong
definition when a name is shadowed. Reusing the AST parse register.py already
does resolves the definition Python actually binds (the last module-level one),
falling back to a nested definition only when there is no module-level one.
"""

from __future__ import annotations

from pathlib import Path

from itest.core.stubgen import STUB_SKIP_LINE
from itest.core.syncer import _status_from_body

IMPL = "    assert 1 + 1 == 2"
STUB = f"    {STUB_SKIP_LINE}"


def _write(tmp_path: Path, body: str, name: str = "test_probe") -> Path:
    path = tmp_path / f"{name}.py"
    path.write_text("import pytest\n\n\n" + body, encoding="utf-8")
    return path


def test_async_implemented_is_implemented(tmp_path: Path) -> None:
    path = _write(tmp_path, f"async def test_x():\n{IMPL}\n")
    assert _status_from_body(path, "test_x") == "implemented"


def test_async_stub_is_stub(tmp_path: Path) -> None:
    path = _write(tmp_path, f"async def test_x():\n{STUB}\n")
    assert _status_from_body(path, "test_x") == "stub"


def test_shadowed_toplevel_uses_the_last_definition(tmp_path: Path) -> None:
    """Two module-level defs of one name: Python binds the last, so the second
    (implemented) is the one registered, not the first (stub)."""
    path = _write(
        tmp_path,
        f"def test_x():\n{STUB}\n\n\ndef test_x():\n{IMPL}\n",
    )
    assert _status_from_body(path, "test_x") == "implemented"


def test_nested_method_does_not_shadow_the_toplevel(tmp_path: Path) -> None:
    """A class method with the same name must not be mistaken for the top-level
    function that is actually registered."""
    nested = f"class TestThing:\n    def test_x(self):\n    {STUB}\n"
    path = _write(tmp_path, f"{nested}\n\ndef test_x():\n{IMPL}\n")
    assert _status_from_body(path, "test_x") == "implemented"


def test_plain_stub_and_impl_still_classify(tmp_path: Path) -> None:
    stub_path = _write(tmp_path, f"def test_x():\n{STUB}\n", name="test_s")
    impl_path = _write(tmp_path, f"def test_x():\n{IMPL}\n", name="test_i")
    assert _status_from_body(stub_path, "test_x") == "stub"
    assert _status_from_body(impl_path, "test_x") == "implemented"


def test_absent_function_is_stub(tmp_path: Path) -> None:
    assert _status_from_body(_write(tmp_path, f"def other():\n{IMPL}\n"), "test_x") == (
        "stub"
    )
