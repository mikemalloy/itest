"""What a declared server's generated checks bind to when pytest runs them.

The generated code holds no logic — an engine module is one parametrized test,
a binding is one call — so everything they need at run time is here, and is
ITest's to fix: the point a check covers, the server it reaches, the engine's
list of cases, and the call into ``itest.checks``.

- :func:`engine_cases` reads ``.itest/manifest.yaml`` when pytest collects an
  engine module and yields one case per (tool, engine trait) in that tier. The
  module never names a tool, so a tool the manifest gains is picked up without
  regenerating anything.
- :func:`itest_point` is the tool point a check covers: an engine case carries
  it; a binding names it in its frozen docstring (``itest point: <id>``).
- :func:`itest_target` is how to reach the point's server, built from its
  declaration. A server that cannot be reached skips the check rather than
  erroring it.
- :func:`run_engine_case` calls ``itest.checks.run_engine_check`` and records
  the ``CheckResult`` on the test (``record_property``), which is how verify's
  ledger reports the check's own status. A check the library does not implement
  yet skips: it was not run, and must not read as an error or a pass. So does a
  ``changed`` or ``not_verifiable`` result, after it is recorded: a finding
  that waits on a human is not a failing check.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, NamedTuple

import pytest

from itest.core.manifest import Manifest, load_manifest

MANIFEST_REL = Path(".itest") / "manifest.yaml"

#: CheckResult statuses that are findings, not failures: ``changed`` waits on a
#: reviewer, ``not_verifiable`` could not be judged. The engine records them and
#: skips, so the page shows them (AT RISK) without pytest failing the point.
NOT_A_FAILURE = ("changed", "not_verifiable")

#: The frozen docstring line of a generated binding.
_DOCSTRING = re.compile(r"itest point: (?P<point>\S+)\s+trait: (?P<trait>\S+)")


class EngineCase(NamedTuple):
    """One (tool, engine trait) for an engine module to run."""

    point: dict[str, Any]
    trait: str


def project_root(start: Path) -> Path:
    """The nearest directory at or above ``start`` holding the manifest."""
    start = Path(start).resolve()
    for directory in (start, *start.parents):
        if (directory / MANIFEST_REL).is_file():
            return directory
    raise pytest.UsageError(
        f"No {MANIFEST_REL} at or above {start}: a generated tool check runs "
        "inside the project that `itest sync` generated it for."
    )


def _manifest(start: Path) -> tuple[Path, Manifest]:
    root = project_root(start)
    return root, load_manifest(root / MANIFEST_REL)


def engine_cases(module_file: str, server: str, tier: str) -> list:
    """Every engine trait of every ``server`` tool in ``tier``, as pytest params.

    Read from the manifest's ``traits_planned`` — what the last sync decided —
    and typed by the trait table. Case ids are ``<tool>-<trait>``, which is the
    node name sync registers, so verify maps each result to its point.
    """
    from itest.core.declarations.traits import load_traits

    _root, manifest = _manifest(Path(module_file).parent)
    table = load_traits()
    cases = []
    for point in manifest.points:
        if point.type != "mcp_tool" or point.source != server:
            continue
        for trait_id in point.traits_planned or []:
            trait = table.get(trait_id)
            if trait is None or trait.kind != "engine" or trait.tier != tier:
                continue
            cases.append(
                pytest.param(
                    EngineCase(point.model_dump(mode="json"), trait_id),
                    id=f"{point.target}-{trait_id}",
                )
            )
    return cases


@pytest.fixture
def itest_point(request) -> dict[str, Any]:
    """The tool point this check covers, as the manifest records it."""
    callspec = getattr(request.node, "callspec", None)
    if callspec is not None and "itest_case" in callspec.params:
        return callspec.params["itest_case"].point
    match = _DOCSTRING.search(request.function.__doc__ or "")
    if match is None:
        pytest.fail(
            f"{request.node.nodeid} has no 'itest point: <id>' line in its "
            "docstring, so itest_point cannot tell which tool it checks."
        )
    _root, manifest = _manifest(Path(str(request.node.path)).parent)
    point = manifest.get_point(match["point"])
    if point is None:
        pytest.fail(
            f"{request.node.nodeid} checks point {match['point']}, which the "
            "manifest does not record. Run `itest plan && itest sync`."
        )
    return point.model_dump(mode="json")


@pytest.fixture
def itest_target(itest_point, request):
    """How to reach the point's server, built from its declaration."""
    from itest.core.declarations import load_declarations, missing_url_message
    from itest.core.declarations.tools import build_target

    root = project_root(Path(str(request.node.path)).parent)
    server = itest_point["source"]
    declaration = next((d for d in load_declarations(root) if d.server == server), None)
    if declaration is None:
        pytest.skip(f"{server} is no longer declared under .itest/tools/")
    target = build_target(declaration, root)
    if target is None:
        pytest.skip(f"{server} {missing_url_message(declaration)}")
    return target


@pytest.fixture
def itest_authenticated(itest_target) -> bool:
    """Whether an authenticated call is possible: the server names a credential."""
    return itest_target.credential_env is not None


def run_engine_case(case: EngineCase, target, *, authenticated: bool, record=None):
    """Run one engine check and record its result on the test.

    ``record`` is pytest's ``record_property``: the result lands in the test's
    ``user_properties`` as ``itest_check``, which verify reads back.
    """
    import itest.checks as checks

    try:
        result = checks.run_engine_check(
            case.trait, case.point, target, authenticated=authenticated
        )
    except NotImplementedError as exc:
        pytest.skip(
            f"engine check {case.trait} is not implemented in itest.checks yet ({exc})"
        )
    if record is not None:
        record(
            "itest_check",
            {
                "status": result.status,
                "detail": result.detail,
                "evidence": result.evidence,
            },
        )
    if result.status in NOT_A_FAILURE:
        # Recorded above, so the ledger reports it as-is; skipped here, so the
        # point reads as unverified rather than failing and blocking a release.
        pytest.skip(f"{result.status}: {result.detail}")
    return result
