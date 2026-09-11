"""The readiness page's data, built from verify JSON and the manifest.

Every field here is derived from one of exactly three sources: the document
`itest verify --output json` emits, the manifest, and — only when ``--since``
is given — a prior manifest. Nothing is illustrative. When a source is absent
the field is ``None`` and the renderer draws a named empty state; it never
substitutes a plausible-looking value.

The agent-tool ledger is verify's ``tools`` section, emitted whenever the
manifest records a declared tool (see ``verifier.build_tool_ledger``). Fields
verify cannot supply today are modelled as ``Optional`` and listed in
`docs/report.md`: the not-analyzed census (it lives in the plan, not in
verify), the release commit, and the per-probe unauthenticated/authenticated
outcomes (verify records a point's status but never which registered test was
the anonymous probe).

Verdict rules
-------------
Evaluated in order; the first that matches wins.

**BLOCKED** — the release is not shippable on this evidence:

* any tool check has status ``critical``, or a server's ``summary.critical``
  is nonzero, or
* any integration point is ``failing`` or ``error``.

**AT RISK** — something needs a human before this is a green release:

* any tool check has status ``changed`` (a declared property moved and no
  reviewer has confirmed it), or
* any tool check is ``stale`` (hand-edited, and generated against a schema the
  tool no longer has), or any declared tool is not verified — VERIFIED is a
  coverage claim, and stale, not-applicable and orphaned checks do not count
  toward it, or
* a point reports ``stub`` while its manifest entry says ``implemented`` —
  the test was written but did not verify anything this run, or
* any point reports ``stub`` at all. A stub is not coverage, so a run with
  unverified points cannot carry a green stamp however few they are.

**VERIFIED** — every point verified and no tool change is waiting on review.

Point statuses come from verify's own precedence (fail > error > pass > stub),
so this never re-derives a status the verifier already decided.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from itest.core.manifest import Manifest

#: The status vocabulary a tool check may carry. Pinned against the committed
#: contract fixture (tests/fixtures/report/tool-ledger.json) by test.
CHECK_STATUSES = (
    "pass",
    "fail",
    "critical",
    "changed",
    "held_out",
    "not_verifiable",
    "n/a",
    # A registered check that did not run: skipped, or not implemented yet.
    "not_run",
)

#: A check's lifecycle state: how far its test can be trusted as a statement
#: about the tool today. Pinned against the contract fixture by test.
#:
#: ``current``        ITest-owned and generated against the current schema
#: ``stale``          hand-edited AND generated against an older schema
#: ``hand_edited``    a human changed the file (its ownership hash differs)
#: ``recipe_newer``   the recipe moved since the check was generated
#: ``not_applicable`` retired: the trait no longer applies to the tool
#: ``orphan``         the tool is gone; the test is kept, never deleted
CHECK_STATES = (
    "current",
    "stale",
    "hand_edited",
    "recipe_newer",
    "not_applicable",
    "orphan",
)

#: States that do not count toward VERIFIED.
UNCOUNTED_STATES = ("stale", "not_applicable", "orphan")

MUTATIONS = ("read", "write", "destructive", "informational")

MUTATION_SOURCES = ("detected", "declared", "confirmed")

#: Point statuses verify emits, mapped to the token the page prints. The page
#: prints what verify recorded and never a richer claim: "PASS" is a passing
#: check, not "the endpoint refused an anonymous caller".
STATUS_TOKEN = {
    "passing": "PASS",
    "failing": "FAIL",
    "error": "ERROR",
    "stub": "STUB",
    "gated": "GATED",
}

#: Result-cell class per status token, matching the template's `.res` classes.
STATUS_CLASS = {
    "PASS": "ok",
    "FAIL": "bad",
    "ERROR": "bad",
    "STUB": "none",
    "GATED": "none",
}

_ARN = re.compile(r"arn:[^:]*:[^:]*:([^:]*):([0-9]{12}|[A-Za-z0-9*-]*):")


class _Strict(BaseModel):
    """Base for the contract models: an unknown field is a failing test."""

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# The tool ledger — EXACTLY the shape of tests/fixtures/report/tool-ledger.json.
# That file is the contract with P31. Field names are final; extend, never
# rename, and the fixture test fails on any drift in either direction.
# ---------------------------------------------------------------------------


class ToolChange(_Strict):
    previous: str
    current: str


class ToolCheck(_Strict):
    trait: str
    status: str
    detail: str
    test: str
    change: ToolChange | None = None
    #: One of :data:`CHECK_STATES`; ``None`` when no test is registered to judge.
    state: str | None = None


class ToolEntry(_Strict):
    # Not a pytest test class despite pydantic's introspection.
    __test__ = False

    name: str
    point_id: str
    mutation: str
    mutation_source: str
    egress: str | None = None
    approval: str | None = None
    held_out: bool = False
    schema_hash: str | None = None
    description_hash: str | None = None
    checks: list[ToolCheck] = Field(default_factory=list)


class ToolFamily(_Strict):
    id: str
    name: str
    checked: int
    passed: int
    not_verifiable: int = 0


class ToolSummary(_Strict):
    declared: int
    live: int
    undeclared: int = 0
    orphaned: int = 0
    verified: int = 0
    changed: int = 0
    held_out: int = 0
    not_verifiable: int = 0
    critical: int = 0


class ToolException(_Strict):
    kind: str
    tool: str
    trait: str | None = None
    message: str


class ToolServer(_Strict):
    server: str
    environment: str | None = None
    run_at: str | None = None
    declaration: str | None = None
    summary: ToolSummary
    families: list[ToolFamily] = Field(default_factory=list)
    tools: list[ToolEntry] = Field(default_factory=list)
    exceptions: list[ToolException] = Field(default_factory=list)


class ToolLedger(_Strict):
    servers: list[ToolServer] = Field(default_factory=list)

    @property
    def declared(self) -> int:
        return sum(s.summary.declared for s in self.servers)

    @property
    def verified(self) -> int:
        return sum(s.summary.verified for s in self.servers)

    @property
    def changed(self) -> int:
        return sum(s.summary.changed for s in self.servers)

    @property
    def critical(self) -> int:
        crit = sum(s.summary.critical for s in self.servers)
        checks = [c for s in self.servers for t in s.tools for c in t.checks]
        return crit + sum(1 for c in checks if c.status == "critical")

    def has_changed_check(self) -> bool:
        checks = [c for s in self.servers for t in s.tools for c in t.checks]
        return self.changed > 0 or any(c.status == "changed" for c in checks)

    def has_stale_check(self) -> bool:
        return any(
            c.state == "stale" for s in self.servers for t in s.tools for c in t.checks
        )

    def state_counts(self, server: ToolServer) -> dict[str, int]:
        counts: dict[str, int] = {}
        for tool in server.tools:
            for check in tool.checks:
                if check.state:
                    counts[check.state] = counts.get(check.state, 0) + 1
        return counts


# ---------------------------------------------------------------------------
# Page sections.
# ---------------------------------------------------------------------------


class Tile(BaseModel):
    """One posture card. ``trend`` is set only when --since was given."""

    n: int
    label: str
    trend: str | None = None
    direction: str | None = None
    zero: bool = False
    attn: bool = False


class Verdict(BaseModel):
    word: str
    integrations_verified: int
    integrations_total: int
    #: route_edge points verify verified, over route_edge points detected.
    endpoints_verified: int
    endpoints_total: int
    #: Needs a probe kind in verify JSON to be honest; None until then.
    endpoints_refuse_anon: int | None = None
    authenticated_200: int | None = None
    authenticated_total: int | None = None
    drift: int = 0
    tools_verified: int | None = None
    tools_total: int | None = None
    tool_changes_to_review: int = 0


class PostureInfra(BaseModel):
    cross_stack: int = 0
    wildcard: int = 0
    broad_managed: int = 0
    unauthenticated_routes: int = 0
    #: Per-count deltas against the prior manifest; empty without --since.
    deltas: dict[str, int] = Field(default_factory=dict)

    def tiles(self) -> list[Tile]:
        """The four infrastructure cards, in the template's order."""
        spec = [
            (
                "cross_stack",
                self.cross_stack,
                "cross-stack dependencies<br>resolved to other stacks",
                False,
            ),
            (
                "wildcard",
                self.wildcard,
                "wildcard-resource grants<br>flagged for review",
                False,
            ),
            (
                "broad_managed",
                self.broad_managed,
                "broad managed policies<br>(e.g. *FullAccess)",
                True,
            ),
            (
                "unauthenticated_routes",
                self.unauthenticated_routes,
                "unauthenticated routes<br>detected in the gateway",
                True,
            ),
        ]
        out: list[Tile] = []
        for key, n, label, zero_is_good in spec:
            tile = Tile(n=n, label=label, zero=zero_is_good and n == 0)
            if key in self.deltas:
                tile.trend, tile.direction = _trend(self.deltas[key])
            out.append(tile)
        return out


class ApiEndpoint(BaseModel):
    method: str
    path: str
    #: The token verify recorded for this point (PASS/FAIL/ERROR/STUB/GATED).
    status: str
    point_id: str
    #: Set only once verify records which probe was which. See module docstring.
    unauth: str | None = None
    auth: str | None = None


class ApiGroup(BaseModel):
    resource: str
    endpoints: list[ApiEndpoint] = Field(default_factory=list)


class ApiSweep(BaseModel):
    groups: list[ApiGroup] = Field(default_factory=list)
    held_out: list[str] = Field(default_factory=list)
    not_routed: list[str] = Field(default_factory=list)
    #: False while verify cannot say which registered test was the anonymous
    #: probe. The renderer then draws ONE status column, never an
    #: Unauthenticated/Authenticated pair holding the same neutral value.
    probe_kind_known: bool = False

    @property
    def verified(self) -> int:
        return sum(1 for g in self.groups for e in g.endpoints if e.status == "PASS")

    @property
    def total(self) -> int:
        return sum(len(g.endpoints) for g in self.groups)


class GraphPoint(BaseModel):
    id: str
    #: The edge's source, short. Carried so the diagram can name its root node
    #: from data instead of assuming every graph hangs off one IAM role.
    src: str
    tgt: str
    tag: str
    wild: bool = False
    ext: bool = False
    hub: bool = False
    is_new: bool = False
    status: str = "STUB"


class GraphChain(BaseModel):
    id: str
    source: str
    target: str
    tag: str
    is_new: bool = False
    status: str = "STUB"


class Graph(BaseModel):
    points: list[GraphPoint] = Field(default_factory=list)
    chain: list[GraphChain] = Field(default_factory=list)


class Footer(BaseModel):
    commit: str | None = None
    run: str
    account: str | None = None
    region: str | None = None
    elapsed_s: float = 0.0
    generated_at: str


class Page(BaseModel):
    verdict: Verdict
    posture: PostureInfra
    tools: ToolLedger | None = None
    tool_tiles: list[Tile] = Field(default_factory=list)
    api: ApiSweep
    graph: Graph
    footer: Footer
    environment: str | None = None
    on_safe_floor: bool = False
    #: The plan's census of resources no detector models. Not in verify JSON.
    not_analyzed: dict[str, int] | None = None
    #: Set only with --since; the prior manifest's date.
    since: str | None = None
    new_points: list[str] = Field(default_factory=list)
    removed_points: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Derivation.
# ---------------------------------------------------------------------------


def _trend(delta: int) -> tuple[str, str]:
    """The trend caption and direction for a delta against the prior release."""
    if delta > 0:
        return f"+{delta}", "up"
    if delta < 0:
        # A minus sign, not a hyphen: this is prose, and it sits next to ↓.
        return f"−{abs(delta)}", "down"
    return "steady", "flat"


def _short_target(label: str) -> str:
    """A readable node name: ARNs keep service and resource, HCL keeps its name.

    Purely presentational, and deliberately local to the page rather than
    borrowed from `itest.core.points`: that module's shortener serves the
    terminal's one-line tags, and the two must be free to differ.
    """
    if label.startswith("arn:"):
        parts = label.split(":", 5)
        service = parts[2] if len(parts) > 2 else "arn"
        resource = parts[5] if len(parts) > 5 else ""
        if not resource:
            return service
        return f"{service}:{resource}"
    return label


def _account_and_region(strings: list[str]) -> tuple[str | None, str | None]:
    """Read the account id and region out of whatever ARNs the report carries.

    After ``--redact`` the report's strings are already pseudonymized, so this
    reads the stand-in — the page never re-derives a real account id.
    """
    regions: set[str] = set()
    accounts: set[str] = set()
    for value in strings:
        for region, account in _ARN.findall(value):
            if region:
                regions.add(region)
            if account and account != "aws":
                accounts.add(account)

    def join(values: set[str]) -> str | None:
        return ", ".join(sorted(values)) if values else None

    return join(accounts), join(regions)


def _posture_counts(points: list[dict], types: dict[str, str]) -> dict[str, int]:
    """The four infrastructure counts, read off point attributes."""
    counts = {
        "cross_stack": 0,
        "wildcard": 0,
        "broad_managed": 0,
        "unauthenticated_routes": 0,
    }
    for point in points:
        attrs = point.get("attributes") or {}
        if attrs.get("external"):
            counts["cross_stack"] += 1
        if attrs.get("wildcard_resource"):
            counts["wildcard"] += 1
        if attrs.get("broad_managed_policy"):
            counts["broad_managed"] += 1
        if types.get(point["id"]) == "route_edge" and attrs.get("auth") == "NONE":
            counts["unauthenticated_routes"] += 1
    return counts


def _manifest_as_points(manifest: Manifest) -> list[dict]:
    """The prior manifest's points in the same shape the counts expect."""
    return [{"id": p.id, "attributes": p.attributes} for p in manifest.points]


def _fixed_segments(path: str) -> list[str]:
    """The path's leading segments, up to its first variable one."""
    segments: list[str] = []
    for segment in path.strip("/").split("/"):
        if segment.startswith("{") or not segment:
            break
        segments.append(segment)
    return segments


def _group_depth(paths: list[str]) -> int:
    """How many path segments to group endpoints by.

    One segment normally. An API that puts everything under a single prefix
    (`/api/...`) would collapse to one group at that depth, which says nothing,
    so the depth grows until the paths actually separate.
    """
    depth = 1
    while depth < 4:
        keys = {tuple(_fixed_segments(p)[:depth]) for p in paths}
        deeper = any(len(_fixed_segments(p)) > depth for p in paths)
        if len(keys) > 1 or not deeper:
            return depth
        depth += 1
    return depth


def _api_resource(path: str, depth: int = 1) -> str:
    """Group key for an endpoint: its first ``depth`` fixed path segments."""
    segments = _fixed_segments(path)[:depth]
    return "/" + "/".join(segments) if segments else path or "/"


def build(
    verify: dict,
    manifest: Manifest,
    prior: Manifest | None = None,
    *,
    generated_at: datetime | None = None,
    commit: str | None = None,
    redacted: bool = False,
) -> Page:
    """Assemble the page's data from a verify JSON document and the manifest.

    ``prior`` is the ``--since`` manifest. Without it no trend, no since-line,
    and no new/removed mark is produced anywhere on the page.
    """
    generated_at = generated_at or datetime.now(UTC)
    verify_points: list[dict] = verify.get("points") or []
    types = {p.id: p.type for p in manifest.points}
    status_of = {p["id"]: STATUS_TOKEN.get(p["status"], "????") for p in verify_points}

    prior_ids = {p.id for p in prior.points} if prior else set()
    current_ids = {p["id"] for p in verify_points}
    new_points = sorted(current_ids - prior_ids) if prior else []
    removed_points = sorted(prior_ids - current_ids) if prior else []

    # --- tools ------------------------------------------------------------
    ledger = None
    tool_tiles: list[Tile] = []
    if verify.get("tools"):
        ledger = ToolLedger.model_validate(verify["tools"])
        for family in (s for server in ledger.servers for s in server.families):
            tool_tiles.append(
                Tile(
                    n=family.passed,
                    label=(
                        f"{family.name} checks passed<br>"
                        f"{family.checked} checked · "
                        f"{family.not_verifiable} not verifiable"
                    ),
                    attn=family.passed < family.checked,
                )
            )

    # --- posture ----------------------------------------------------------
    counts = _posture_counts(verify_points, types)
    posture = PostureInfra(**counts)
    if prior is not None:
        prior_counts = _posture_counts(
            _manifest_as_points(prior), {p.id: p.type for p in prior.points}
        )
        posture.deltas = {k: counts[k] - prior_counts[k] for k in counts}

    # --- API sweep --------------------------------------------------------
    route_points = [p for p in verify_points if types.get(p["id"]) == "route_edge"]
    depth = _group_depth(
        [(p.get("attributes") or {}).get("path") or "" for p in route_points]
    )
    grouped: dict[str, list[ApiEndpoint]] = {}
    for point in route_points:
        attrs = point.get("attributes") or {}
        path = attrs.get("path") or ""
        endpoint = ApiEndpoint(
            method=attrs.get("method") or "?",
            path=path,
            status=status_of[point["id"]],
            point_id=point["id"],
        )
        grouped.setdefault(_api_resource(path, depth), []).append(endpoint)
    api = ApiSweep(
        groups=[
            ApiGroup(resource=resource, endpoints=endpoints)
            for resource, endpoints in sorted(grouped.items())
        ]
    )

    # --- graph ------------------------------------------------------------
    graph = Graph()
    for point in verify_points:
        kind = types.get(point["id"])
        attrs = point.get("attributes") or {}
        if kind == "iam_edge":
            graph.points.append(
                GraphPoint(
                    id=point["id"],
                    src=_short_target(point["source"]),
                    tgt=_short_target(point["target"]),
                    tag=point.get("tag", ""),
                    wild=bool(attrs.get("wildcard_resource")),
                    ext=bool(attrs.get("external")),
                    is_new=point["id"] in new_points,
                    status=status_of[point["id"]],
                )
            )
        elif kind == "event_edge":
            graph.chain.append(
                GraphChain(
                    id=point["id"],
                    source=_short_target(point["source"]),
                    target=_short_target(point["target"]),
                    tag=point.get("tag", ""),
                    is_new=point["id"] in new_points,
                    status=status_of[point["id"]],
                )
            )
    # The hub is the resource the event chain hangs off — marked only when a
    # chain edge genuinely starts at one of the graph's own targets.
    chain_sources = {c.source for c in graph.chain}
    for node in graph.points:
        if node.tgt in chain_sources:
            node.hub = True

    # --- verdict ----------------------------------------------------------
    implemented_points = {
        t.point_id
        for t in manifest.tests
        if t.status == "implemented" and not t.disabled and not t.retired
    }
    statuses = [p["status"] for p in verify_points]
    stuck = any(
        p["status"] == "stub" and p["id"] in implemented_points for p in verify_points
    )
    if (ledger and ledger.critical) or any(s in ("failing", "error") for s in statuses):
        word = "BLOCKED"
    elif (
        (ledger and ledger.has_changed_check())
        or (ledger and ledger.has_stale_check())
        or (ledger and ledger.verified < ledger.declared)
        or stuck
        or any(s == "stub" for s in statuses)
    ):
        word = "AT RISK"
    else:
        word = "VERIFIED"

    verdict = Verdict(
        word=word,
        integrations_verified=int(verify.get("passing", 0)),
        integrations_total=int(verify.get("total_points", 0)),
        endpoints_verified=api.verified,
        endpoints_total=api.total,
        drift=int(verify.get("orphaned_tests", 0))
        + len(verify.get("unregistered") or []),
        tools_verified=ledger.verified if ledger else None,
        tools_total=ledger.declared if ledger else None,
        tool_changes_to_review=ledger.changed if ledger else 0,
    )

    # --- footer -----------------------------------------------------------
    account, region = _account_and_region(
        [p.get("source", "") + " " + p.get("target", "") for p in verify_points]
    )
    run = "verify"
    if verify.get("environment"):
        run += f" --environment {verify['environment']}"
    if redacted:
        run += " --redact"
    footer = Footer(
        commit=commit,
        run=run,
        account=account,
        region=region,
        elapsed_s=float(verify.get("elapsed_seconds", 0.0)),
        generated_at=generated_at.strftime("%Y-%m-%d %H:%M UTC"),
    )

    return Page(
        verdict=verdict,
        posture=posture,
        tools=ledger,
        tool_tiles=tool_tiles,
        api=api,
        graph=graph,
        footer=footer,
        environment=verify.get("environment"),
        on_safe_floor=bool(verify.get("on_safe_floor")),
        since=prior.generated_at.strftime("%Y-%m-%d") if prior else None,
        new_points=new_points,
        removed_points=removed_points,
    )
