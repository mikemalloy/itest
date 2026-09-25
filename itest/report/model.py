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
Four words, evaluated in this order; the first that matches wins. The one
derivation is :func:`derive_verdict`, which also writes the plain sentence
(``Verdict.reason``) the page shows beneath the word, so nothing downstream
re-derives meaning from counts.

**BLOCKED** — a finding. Red.

* any tool check has status ``critical`` or ``fail``, or a server's
  ``summary.critical`` is nonzero, or
* any integration point is ``failing`` or ``error``.

**NEEDS REVIEW** — a human decision is pending. Amber.

* any tool check has status ``changed`` (a declared property moved and no
  reviewer has confirmed it), or
* any tool check is ``stale`` (generated against a schema the tool no longer
  has, and nobody has re-read it since), or
* a point reports ``stub`` while its manifest entry says ``implemented`` —
  the test was written but did not verify anything this run.

**PARTIAL** — nothing failed and nothing is pending, but the page cannot claim
full coverage. Nothing is wrong that we know of; we did not look at
everything. Grey-blue, never amber, never green.

* any tool check is ``held_out`` (the environment policy withheld its tier),
  ``not_verifiable`` (this server does not declare what it needs, or the
  check is not written yet) or ``not_run``, or
* any declared tool is not verified — VERIFIED is a coverage claim, and
  stale, not-applicable and orphaned checks do not count toward it, or
* any point reports ``stub`` or ``gated``. A stub is not coverage, so a run
  with unverified points cannot carry a green stamp however few they are.

**VERIFIED** — every declared property of every declared tool passed, every
point verified, and nothing is waiting on review. Green. Never a bare green.

Point statuses come from verify's own precedence (fail > error > pass > stub),
so this never re-derives a status the verifier already decided.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from itest.core import lifecycle
from itest.core.manifest import EvidenceRecord, Manifest, SourceRecord
from itest.traits.ids import migrate_family_id, migrate_trait_id

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

#: The verdict vocabulary, in precedence order. :func:`derive_verdict` is the
#: one place a word is chosen.
VERDICT_WORDS = ("BLOCKED", "NEEDS REVIEW", "PARTIAL", "VERIFIED")

#: A check's lifecycle state, and the ones that do not count toward VERIFIED.
#: Defined once in ``itest.core.lifecycle``; re-exported here for the page.
CHECK_STATES = lifecycle.CHECK_STATES
UNCOUNTED_STATES = lifecycle.UNCOUNTED_STATES

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
    #: The trait's slug (``authority.anonymous``). A ledger written with the old
    #: AN-style ids is read with the new ones.
    trait: str
    status: str
    detail: str
    test: str
    change: ToolChange | None = None
    #: One of :data:`CHECK_STATES`; ``None`` when no test is registered to judge.
    state: str | None = None
    #: The trait's display code (``AUTH-1``) — a label, never an identity.
    code: str | None = None
    #: The published ids the trait answers (``ASI03``, ``semgrep-server-4``).
    standards: list[str] = Field(default_factory=list)

    @field_validator("trait", mode="before")
    @classmethod
    def _current_trait_id(cls, value: object) -> object:
        return migrate_trait_id(value) if isinstance(value, str) else value


class EvidenceLane(_Strict):
    """External evidence on one tool row, derived from the manifest's
    :class:`~itest.core.manifest.EvidenceRecord`.

    A display record and nothing more: it has no status, no state and no
    test, so nothing that reads a check can read it. The rate is its parts —
    ``calls N · refused R · of T rows`` — never a percentage alone.
    """

    source: str
    kind: str
    agent: str | None = None
    run_id: str | None = None
    run_at: str | None = None
    #: What sync decided with its own clock; never recomputed here.
    stale: bool = False
    rate: str
    #: ``targeted in N of T rows``, or the note that the harness declared none.
    targeted: str
    #: The source's declared ids: shown as external evidence, never coverage.
    standards: list[str] = Field(default_factory=list)


class SourceLine(_Strict):
    """One declared source as the last sync read it, for the section header."""

    name: str
    kind: str | None = None
    server: str | None = None
    status: str
    reason: str | None = None
    run_id: str | None = None
    run_at: str | None = None
    shape: str = "none"
    rows: int = 0
    matched_tools: list[str] = Field(default_factory=list)
    unmatched_tools: list[str] = Field(default_factory=list)
    stale: bool = False
    agent: str | None = None
    standards: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


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
    #: The evidence lane beneath this row. Never in verify JSON: attached from
    #: the manifest by :func:`build`, and read by nothing that counts.
    evidence: list[EvidenceLane] = Field(default_factory=list)


class ToolFamily(_Strict):
    #: The family id (``authority``); an old family letter is read as its id.
    id: str
    name: str
    checked: int
    passed: int
    not_verifiable: int = 0

    @field_validator("id", mode="before")
    @classmethod
    def _current_family_id(cls, value: object) -> object:
        return migrate_family_id(value) if isinstance(value, str) else value


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

    @field_validator("trait", mode="before")
    @classmethod
    def _current_trait_id(cls, value: object) -> object:
        return migrate_trait_id(value) if isinstance(value, str) else value


class ToolServer(_Strict):
    server: str
    environment: str | None = None
    run_at: str | None = None
    declaration: str | None = None
    summary: ToolSummary
    families: list[ToolFamily] = Field(default_factory=list)
    tools: list[ToolEntry] = Field(default_factory=list)
    exceptions: list[ToolException] = Field(default_factory=list)
    #: The declared sources about this server, from the manifest.
    sources: list[SourceLine] = Field(default_factory=list)


class StandardsEntry(_Strict):
    """One published id as the ledger's rollup carries it (``tools.standards``)."""

    id: str
    title: str
    framework: str
    coverage: str
    note: str | None = None
    checks: int = 0
    statuses: dict[str, int] = Field(default_factory=dict)


class ToolLedger(_Strict):
    servers: list[ToolServer] = Field(default_factory=list)
    #: The standards lens over the checks above. Emitted by verify; a ledger
    #: written before it existed gets one derived the same way (see ``build``).
    standards: list[StandardsEntry] = Field(default_factory=list)

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


class Tally(BaseModel):
    """The counts the verdict's sentence is built from.

    A *check* is one :class:`ToolCheck` cell — the unit the tools grid counts
    — plus one integration point that is not itself a declared tool (a tool
    point's checks are its cells, so the point is not counted twice). Retired
    and orphaned cells are not checks on a tool as it is today and are left
    out of the total.
    """

    total: int = 0
    passed: int = 0
    #: Held out, not verifiable or not run: cells nobody looked at here, and
    #: points still at stub or gated.
    not_run: int = 0
    #: Critical and failing cells; failing and errored points.
    findings: int = 0
    #: Changed and stale cells; points stuck at stub though implemented.
    pending: int = 0


class Verdict(BaseModel):
    #: One of :data:`VERDICT_WORDS`.
    word: str
    #: The plain sentence beneath the word, written by :func:`derive_verdict`.
    reason: str = ""
    tally: Tally = Field(default_factory=Tally)
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


class Finding(BaseModel):
    """One line of the Answer's findings list: a critical or failing check,
    or a failing or errored point. The check is named by the trait's human
    name from the trait table, never its slug."""

    #: ``critical``, ``failing`` or ``error``.
    severity: str
    tool: str
    check: str
    detail: str | None = None


class RedTeamLine(BaseModel):
    """The Answer's one line per red-team source. Never a verdict input."""

    source: str
    text: str
    #: A write or destructive tool was called.
    warning: bool = False


class Answer(BaseModel):
    """The plain-language layer at the top of the page: is there a security
    problem? Derived from the same data as everything below the divider and
    written in none of the method vocabulary the detail layer relies on.
    """

    word: str
    sentence: str
    findings: list[Finding] = Field(default_factory=list)
    #: ``Ran: Change — 24 of 24 passed.`` or ``None`` when nothing ran.
    ran: str | None = None
    #: ``Not run here: Authority, … — <why>.``: one line per reason.
    not_run: list[str] = Field(default_factory=list)
    red_team: list[RedTeamLine] = Field(default_factory=list)
    footer: str = "Detail below."


class Page(BaseModel):
    verdict: Verdict
    answer: Answer
    posture: PostureInfra
    tools: ToolLedger | None = None
    #: The two former hero tiles, now in the posture section's grids: agent
    #: tools verified leads ``tool_tiles``; this one leads the infrastructure
    #: grid.
    integration_tile: Tile = Field(default_factory=lambda: Tile(n=0, label=""))
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
    #: Source lines naming no server the ledger has (a file that did not parse,
    #: a server the manifest does not inventory). Still shown, never dropped.
    unattached_sources: list[SourceLine] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Derivation.
# ---------------------------------------------------------------------------

#: What the lane says when no row of the run declared a target tool.
TARGETING_NOT_DECLARED = "targeting not declared by the harness"


def evidence_lane(record: EvidenceRecord, source: SourceRecord | None) -> EvidenceLane:
    """The display record for one EvidenceRecord. ``source`` is its line, for
    the declared standards; ``None`` when the manifest lost it."""
    if record.targeted is None:
        targeted = TARGETING_NOT_DECLARED
    else:
        targeted = f"targeted in {record.targeted} of {record.rows_total} rows"
    return EvidenceLane(
        source=record.source_name,
        kind=record.kind,
        agent=record.agent,
        run_id=record.run_id,
        run_at=record.run_at,
        stale=record.stale,
        rate=(
            f"calls {record.calls} · refused {record.refused} · of "
            f"{record.rows_total} rows"
        ),
        targeted=targeted,
        standards=list(source.standards) if source is not None else [],
    )


def source_line(record: SourceRecord) -> SourceLine:
    return SourceLine.model_validate(record.model_dump())


def _attach_evidence(ledger: ToolLedger, manifest: Manifest) -> list[SourceLine]:
    """Hang the manifest's evidence lane on the ledger's rows and servers.

    Touches ``ToolEntry.evidence`` and ``ToolServer.sources`` only — never a
    check, a summary or a family — so every count the ledger derives is what
    it was before. Returns the source lines no server could claim.
    """
    by_name = {s.name: s for s in manifest.sources}
    by_point: dict[str, list[EvidenceRecord]] = {}
    for record in manifest.evidence:
        by_point.setdefault(record.point_id, []).append(record)
    for server in ledger.servers:
        for tool in server.tools:
            tool.evidence = [
                evidence_lane(record, by_name.get(record.source_name))
                for record in sorted(
                    by_point.get(tool.point_id, []), key=lambda r: r.source_name
                )
            ]
    servers = {s.server: s for s in ledger.servers}
    unattached: list[SourceLine] = []
    for record in manifest.sources:
        server = servers.get(record.server or "")
        if server is None:
            unattached.append(source_line(record))
        else:
            server.sources.append(source_line(record))
    return unattached


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


#: Cell statuses by what they mean for the tally. ``n/a`` is a retired check
#: and is left out of the total; anything unknown is treated as not run.
_FINDING_STATUSES = ("critical", "fail")
_PENDING_STATUSES = ("changed",)
_PASSED_STATUSES = ("pass",)


def tally_checks(
    points: list[dict],
    types: dict[str, str],
    ledger: ToolLedger | None,
    implemented: set[str],
) -> Tally:
    """Count every check on the page into the five buckets of :class:`Tally`."""
    tally = Tally()
    if ledger is not None:
        critical_cells = 0
        changed_cells = 0
        for server in ledger.servers:
            for tool in server.tools:
                for check in tool.checks:
                    if check.state in ("not_applicable", "orphan") or (
                        check.status == "n/a"
                    ):
                        continue
                    tally.total += 1
                    if check.status in _FINDING_STATUSES:
                        tally.findings += 1
                        critical_cells += check.status == "critical"
                    elif check.status in _PENDING_STATUSES or check.state == "stale":
                        tally.pending += 1
                        changed_cells += check.status == "changed"
                    elif check.status in _PASSED_STATUSES:
                        tally.passed += 1
                    else:
                        tally.not_run += 1
        # A summary can record what its cells do not list (an older ledger).
        summary_critical = sum(s.summary.critical for s in ledger.servers)
        tally.findings += max(0, summary_critical - critical_cells)
        tally.pending += max(0, ledger.changed - changed_cells)
    for point in points:
        if types.get(point["id"]) == "mcp_tool":
            continue  # its checks are the cells above
        tally.total += 1
        status = point["status"]
        if status == "passing":
            tally.passed += 1
        elif status in ("failing", "error"):
            tally.findings += 1
        elif status == "stub" and point["id"] in implemented:
            tally.pending += 1
        else:
            tally.not_run += 1
    return tally


def derive_verdict(
    points: list[dict],
    types: dict[str, str],
    ledger: ToolLedger | None,
    implemented: set[str],
) -> tuple[str, str, Tally]:
    """The verdict word, its plain sentence, and the counts behind them.

    The four words, in precedence order — the first that applies wins:

    ``BLOCKED``       a finding: a critical or failing check, a failing or
                      errored point. Nothing else matters until it is fixed.
    ``NEEDS REVIEW``  a human decision is pending: a declared property changed
                      and no reviewer confirmed it, a check is stale, or a
                      point reports stub while its manifest entry says
                      implemented.
    ``PARTIAL``       nothing failed and nothing is pending, but the page
                      cannot claim full coverage: checks were held out by the
                      environment policy, are not verifiable for this server,
                      or are not written yet; or a declared tool is not
                      verified. Nothing is wrong that we know of; we did not
                      look at everything. Never amber, never green.
    ``VERIFIED``      every declared property of every declared tool passed,
                      every point verified, nothing waiting on review. The
                      only word that is ever green.

    The sentence is chosen by the word and nothing else; the page shows it
    verbatim rather than rebuilding meaning from the numbers.
    """
    tally = tally_checks(points, types, ledger, implemented)
    if tally.findings:
        word = "BLOCKED"
        verb = "needs" if tally.findings == 1 else "need"
        reason = f"{_count(tally.findings, 'finding')} {verb} attention before release."
    elif tally.pending:
        word = "NEEDS REVIEW"
        verb = "is" if tally.pending == 1 else "are"
        reason = (
            f"No findings. {_count(tally.pending, 'tool change')} {verb} waiting "
            "for a reviewer."
        )
    elif tally.not_run or (ledger is not None and ledger.verified < ledger.declared):
        word = "PARTIAL"
        verb = "was" if tally.not_run == 1 else "were"
        reason = (
            f"No findings. {tally.passed} of {_count(tally.total, 'check')} passed; "
            f"{tally.not_run} {verb} not run here."
        )
    else:
        word = "VERIFIED"
        reason = f"No findings. All {_count(tally.total, 'check')} passed."
    return word, reason, tally


#: The three reasons a check was not run here, as the Answer states them.
WHY_CREDENTIALS = "these need a staging environment with credentials"
WHY_UNDECLARED = "this server does not declare what they need"
WHY_UNWRITTEN = "the check exists but has not been written yet"
_WHY_ORDER = (WHY_CREDENTIALS, WHY_UNDECLARED, WHY_UNWRITTEN)

#: Family id -> human name, when the ledger did not carry one.
FAMILY_NAMES = {
    "authority": "Authority",
    "blast": "Blast radius",
    "containment": "Containment",
    "change": "Change",
}

#: The Answer's family for integration points that are not declared tools.
INTEGRATIONS_FAMILY = "Integrations"

_SEVERITY_ORDER = {"critical": 0, "failing": 1, "error": 2}


def _trait_names() -> dict[str, str]:
    """Trait slug -> the table's human name (``destructive gating``)."""
    from itest.core.declarations.traits import load_traits

    return {trait.id: trait.name for trait in load_traits().traits}


def _why_not_run(check: ToolCheck) -> tuple[str, str | None]:
    """The reason a cell was not run here, and the missing fact if the check
    recorded one."""
    if check.status == "held_out":
        return WHY_CREDENTIALS, None
    if check.status == "not_verifiable":
        if check.detail.startswith("no engine check for"):
            return WHY_UNWRITTEN, None
        return WHY_UNDECLARED, check.detail or None
    return WHY_UNWRITTEN, None


def _point_label(point: dict) -> str:
    return (
        point.get("tag") or f"{point.get('source', '?')} -> {point.get('target', '?')}"
    )


def build_answer(
    verify_points: list[dict],
    types: dict[str, str],
    ledger: ToolLedger | None,
    manifest: Manifest,
    verdict: Verdict,
) -> Answer:
    """The Answer block, from the same data as the rest of the page.

    Findings are every critical or failing cell and every failing or errored
    point, critical first. "Ran" and "Not run here" count the same cells the
    grid counts, by family, with one of exactly three reasons for a cell
    nobody looked at. The red-team line is one sentence per source that was
    read, in the template the source's data supports — and it is read from
    the evidence lane only, never from a check, so it can inform the sentence
    and never the word.
    """
    names = _trait_names()
    findings: list[Finding] = []
    ran: dict[str, list[int]] = {}  # family -> [passed, ran]
    not_run: dict[str, dict[str, int]] = {w: {} for w in _WHY_ORDER}
    facts: list[str] = []
    # Families are listed in the ledger's own order, integrations last.
    order: list[str] = []

    if ledger is not None:
        for server in ledger.servers:
            family_names = {f.id: f.name for f in server.families}
            order.extend(n for n in family_names.values() if n not in order)
            for tool in server.tools:
                for check in tool.checks:
                    if check.state in ("not_applicable", "orphan") or (
                        check.status == "n/a"
                    ):
                        continue
                    family_id = check.trait.partition(".")[0]
                    family = family_names.get(family_id) or FAMILY_NAMES.get(
                        family_id, family_id.capitalize()
                    )
                    if check.status in ("critical", "fail"):
                        findings.append(
                            Finding(
                                severity=(
                                    "critical"
                                    if check.status == "critical"
                                    else "failing"
                                ),
                                tool=tool.name,
                                check=names.get(check.trait, check.code or check.trait),
                                detail=check.detail or None,
                            )
                        )
                        ran.setdefault(family, [0, 0])[1] += 1
                    elif check.status in ("pass", "changed"):
                        counts = ran.setdefault(family, [0, 0])
                        counts[1] += 1
                        counts[0] += check.status == "pass" and check.state != "stale"
                    else:
                        why, fact = _why_not_run(check)
                        not_run[why][family] = not_run[why].get(family, 0) + 1
                        if fact and fact not in facts:
                            facts.append(fact)

    for point in verify_points:
        if types.get(point["id"]) == "mcp_tool":
            continue
        status = point["status"]
        if status == "passing":
            counts = ran.setdefault(INTEGRATIONS_FAMILY, [0, 0])
            counts[0] += 1
            counts[1] += 1
        elif status in ("failing", "error"):
            findings.append(
                Finding(severity=status, tool=_point_label(point), check="integration")
            )
            ran.setdefault(INTEGRATIONS_FAMILY, [0, 0])[1] += 1
        else:
            why = WHY_CREDENTIALS if status == "gated" else WHY_UNWRITTEN
            bucket = not_run[why]
            bucket[INTEGRATIONS_FAMILY] = bucket.get(INTEGRATIONS_FAMILY, 0) + 1

    findings.sort(key=lambda f: _SEVERITY_ORDER.get(f.severity, 9))

    def in_order(families: dict[str, int]) -> str:
        rank = {name: i for i, name in enumerate(order)}
        return ", ".join(sorted(families, key=lambda n: rank.get(n, len(rank))))

    ran_line = None
    if ran:
        passed = sum(v[0] for v in ran.values())
        total = sum(v[1] for v in ran.values())
        ran_line = f"Ran: {in_order(ran)} — {passed} of {total} passed."
    not_run_lines = []
    for why in _WHY_ORDER:
        families = not_run[why]
        if not families:
            continue
        reason = why
        if why == WHY_UNDECLARED and facts:
            reason = f"{why}: {'; '.join(facts)}"
        not_run_lines.append(f"Not run here: {in_order(families)} — {reason}.")

    return Answer(
        word=verdict.word,
        sentence=verdict.reason,
        findings=findings,
        ran=ran_line,
        not_run=not_run_lines,
        red_team=_red_team_lines(ledger, manifest),
    )


def _trend(delta: int) -> tuple[str, str]:
    """The trend caption and direction for a delta against the prior release."""
    if delta > 0:
        return f"+{delta}", "up"
    if delta < 0:
        # A minus sign, not a hyphen: this is prose, and it sits next to ↓.
        return f"−{abs(delta)}", "down"
    return "steady", "flat"


def _red_team_lines(ledger: ToolLedger | None, manifest: Manifest) -> list[RedTeamLine]:
    """One line per source that was read. Template (a) when the harness
    declared what it was aiming at (any record carries ``targeted``);
    template (b) otherwise, where the last clause is computed from the
    called tools' mutation classes and never claims intent."""
    tool_of = {p.id: p.target for p in manifest.points}
    mutation_of = {p.id: (p.attributes or {}).get("mutation") for p in manifest.points}
    if ledger is not None:
        for server in ledger.servers:
            for tool in server.tools:
                mutation_of[tool.point_id] = tool.mutation
    lines: list[RedTeamLine] = []
    for line in manifest.sources:
        if line.status != "read":
            continue
        records = sorted(
            (r for r in manifest.evidence if r.source_name == line.name),
            key=lambda r: tool_of.get(r.point_id, r.point_id),
        )
        run_at = line.run_at or ""
        date = run_at[:10] if len(run_at) >= 10 else "undated"
        text = f"Red team ({date}): {_count(line.rows, 'attempt')}."
        warning = False
        targeted = [r for r in records if r.targeted]
        if targeted:
            clauses = []
            for record in targeted:
                name = tool_of.get(record.point_id, record.point_id)
                clause = (
                    f"{record.targeted} targeted {name}; "
                    f"{record.rows_with_call} induced a call"
                )
                if record.rows_with_call:
                    clause += f"; {record.refused} of those were refused by the tool"
                clauses.append(clause)
            text += " " + "; ".join(clauses) + "."
        else:
            called = [r for r in records if r.calls > 0]
            exercised = len(called) + len(line.unmatched_tools)
            text += f" {_count(exercised, 'tool')} exercised."
            bad = [
                tool_of.get(r.point_id, r.point_id)
                for r in called
                if mutation_of.get(r.point_id) in ("write", "destructive")
            ]
            if bad:
                verb = "was" if len(bad) == 1 else "were"
                text += (
                    f" {_count(len(bad), 'write/destructive tool')} {verb} called: "
                    f"{', '.join(bad)}."
                )
                warning = True
            else:
                text += " No write or destructive tool was called."
            if line.unmatched_tools:
                unknown = len(line.unmatched_tools)
                verb = "is" if unknown == 1 else "are"
                text += (
                    f" {_count(unknown, 'called tool')} {verb} not in the tool list "
                    "and could not be classified."
                )
        if line.stale:
            text += f" (stale — from {date})"
        lines.append(RedTeamLine(source=line.name, text=text, warning=warning))
    return lines


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
    unattached: list[SourceLine] = []
    if verify.get("tools"):
        ledger = ToolLedger.model_validate(verify["tools"])
        if not ledger.standards:
            # An older verify JSON: derive the rollup through verify's own
            # function, from the checks the document does carry.
            from itest.traits.standards import rollup_for_ledger

            ledger.standards = [
                StandardsEntry.model_validate(entry)
                for entry in rollup_for_ledger(verify["tools"])
            ]
        unattached = _attach_evidence(ledger, manifest)
        # The former hero tile, now leading the agent-tools grid.
        tool_tiles.append(
            Tile(
                n=ledger.verified,
                label=f"agent tools verified<br>of {ledger.declared} declared",
                attn=ledger.verified < ledger.declared,
            )
        )
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
    word, reason, tally = derive_verdict(
        verify_points, types, ledger, implemented_points
    )

    verdict = Verdict(
        word=word,
        reason=reason,
        tally=tally,
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

    # The other former hero tile, now leading the infrastructure grid.
    integration_tile = Tile(
        n=verdict.integrations_verified,
        label=(
            "integrations verified<br>of "
            f"{_count(verdict.integrations_total, 'point')} detected"
        ),
        attn=verdict.integrations_verified < verdict.integrations_total,
    )

    return Page(
        verdict=verdict,
        answer=build_answer(verify_points, types, ledger, manifest, verdict),
        posture=posture,
        tools=ledger,
        integration_tile=integration_tile,
        tool_tiles=tool_tiles,
        api=api,
        graph=graph,
        footer=footer,
        environment=verify.get("environment"),
        on_safe_floor=bool(verify.get("on_safe_floor")),
        since=prior.generated_at.strftime("%Y-%m-%d") if prior else None,
        new_points=new_points,
        removed_points=removed_points,
        unattached_sources=unattached,
    )
