"""Inject the page's data into the committed template.

The template owns every pixel; this module owns none. It replaces seven exact
markers — ``<!-- itest:data:PAGE -->`` and one per JS constant — with `const`
declarations built from a `model.Page`, and touches nothing else. There is no
templating language and no third-party dependency: a marker is found by exact
string match and replaced once.

The template carries no sample data of its own, so a section with no data
renders as a named empty state rather than as whatever the design artifact
happened to illustrate it with.
"""

from __future__ import annotations

import html
import json
import re
from importlib import resources
from pathlib import Path

from itest.report.model import STATUS_CLASS, Page, Tile

#: The committed template: a resource of the ``itest.report`` package, read
#: through :mod:`importlib.resources` so an installed wheel finds it too.
TEMPLATE_PACKAGE = "itest.report"
TEMPLATE_RESOURCE = "templates/readiness.html"

MARKER = "<!-- itest:data:{name} -->"

#: The JS constant each marker declares. Names are the template's, not ours.
BLOCKS = {
    "PAGE": "PAGE",
    "TOOLS": "TOOLS",
    "TOOLPOSTURE": "toolPosture",
    "SWEEP": "SWEEP",
    "POSTURE": "posture",
    "POINTS": "POINTS",
    "CHAIN": "CHAIN",
}

#: Tool-check status -> the token printed and the result-cell class. The
#: vocabulary is the contract fixture's; anything outside it renders as itself
#: in the neutral class rather than being dropped.
CHECK_CELL = {
    "pass": ("PASS", "ok"),
    "fail": ("FAIL", "bad"),
    "critical": ("CRITICAL", "bad"),
    "changed": ("CHANGED", "warn"),
    "held_out": ("HELD OUT", "warn"),
    "not_verifiable": ("NOT VERIFIABLE", "none"),
    "n/a": ("N/A", "none"),
}

#: Mutation class -> (group heading, css suffix, the label printed in the row).
MUTATION_GROUP = {
    "destructive": ("Destructive", "del", "destructive"),
    "write": ("Writes", "write", "write"),
    "egress": ("External data", "egress", "read + egress"),
    "read": ("Reads", "read", "read"),
    "informational": ("Informational", "info", "informational"),
}

GROUP_ORDER = ["destructive", "write", "egress", "read", "informational"]

_TREND_SYMBOL = {"up": "↑", "down": "↓", "flat": "→"}

_CHECK_GLYPH = (
    '<svg viewBox="0 0 24 24" width="26" height="26" fill="none">'
    '<path d="M20 6.5 9.4 17 4 11.6" stroke="currentColor" stroke-width="2.6" '
    'stroke-linecap="round" stroke-linejoin="round"/></svg>'
)
_WARN_GLYPH = (
    '<svg viewBox="0 0 24 24" width="26" height="26" fill="none">'
    '<path d="M12 3 2 20h20L12 3z" stroke="currentColor" stroke-width="2.2" '
    'stroke-linejoin="round"/><path d="M12 10v4" stroke="currentColor" '
    'stroke-width="2.2" stroke-linecap="round"/>'
    '<circle cx="12" cy="17" r="1.2" fill="currentColor"/></svg>'
)

VERDICT_CLASS = {"VERIFIED": "", "AT RISK": "risk", "BLOCKED": "blocked"}

_BLOCK_RE = re.compile(
    r"^const (?P<name>PAGE|TOOLS|toolPosture|SWEEP|posture|POINTS|CHAIN) = "
    r"(?P<json>.*?);$",
    re.MULTILINE | re.DOTALL,
)


def _js(value: object) -> str:
    """JSON safe to sit inside a ``<script>`` block.

    ``<`` and ``>`` are escaped so no data-carried string can close the tag,
    and the two Unicode line terminators JS treats as newlines are escaped so
    a resource name cannot break the statement.
    """
    text = json.dumps(value, ensure_ascii=False, sort_keys=False)
    return (
        text.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )


def _h(text: object) -> str:
    """HTML-escape a value bound for one of the few raw-markup strings."""
    return html.escape(str(text), quote=False)


def _tile(tile: Tile) -> dict:
    """One posture card. ``trend`` stays absent unless --since supplied one."""
    out: dict = {"n": tile.n, "lab": tile.label}
    if tile.attn:
        out["attn"] = True
    if tile.zero:
        out["zero"] = True
    if tile.trend is not None:
        out["trend"] = tile.trend
        out["dir"] = tile.direction
        out["sym"] = _TREND_SYMBOL.get(tile.direction or "flat", "→")
    return out


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


# ---------------------------------------------------------------------------
# Section builders. Each returns plain data; none of them knows any markup
# beyond the class names the template's CSS already defines.
# ---------------------------------------------------------------------------


def _verdict_block(page: Page) -> dict:
    verdict = page.verdict
    nums: list[dict] = []
    if verdict.tools_total is not None:
        nums.append(
            {
                "value": verdict.tools_verified,
                "total": verdict.tools_total,
                "label": "agent tools verified",
                "cls": "tools",
                "word": VERDICT_CLASS.get(verdict.word, ""),
            }
        )
    nums.append(
        {
            "value": verdict.integrations_verified,
            "total": verdict.integrations_total,
            "label": "integrations verified",
        }
    )
    if verdict.endpoints_total:
        nums.append(
            {
                "value": verdict.endpoints_verified,
                "total": verdict.endpoints_total,
                "label": "endpoints verified",
            }
        )
    # Only rendered once verify records which probe was the anonymous one.
    if verdict.endpoints_refuse_anon is not None:
        nums.append(
            {
                "value": verdict.endpoints_refuse_anon,
                "total": verdict.endpoints_total,
                "label": "endpoints refuse anon",
            }
        )
    if verdict.authenticated_200 is not None:
        nums.append(
            {
                "value": verdict.authenticated_200,
                "total": verdict.authenticated_total,
                "label": "authenticated 200",
            }
        )
    nums.append({"value": verdict.drift, "total": None, "label": "drift"})
    for entry in nums[1:]:
        entry["sep"] = True

    environment = (
        f"<b>{_h(page.environment)}</b>"
        if page.environment
        else "<b>no environment bound</b>"
    )
    floor = " &nbsp;·&nbsp; safe floor (static, readonly)" if page.on_safe_floor else ""
    points = _plural(verdict.integrations_total, "integration point")
    sub = f"{environment} — {points}{floor}"

    since = None
    if page.since:
        since = {
            "label": page.since,
            "added": len(page.new_points),
            "removed": len(page.removed_points),
            "review": (
                _plural(verdict.tool_changes_to_review, "tool change") + " to review"
                if verdict.tool_changes_to_review
                else ""
            ),
        }

    return {
        "word": verdict.word,
        "cls": VERDICT_CLASS.get(verdict.word, ""),
        "glyph": _CHECK_GLYPH if verdict.word == "VERIFIED" else _WARN_GLYPH,
        "sub": sub,
        "nums": nums,
        "note": _verdict_note(page),
        "since": since,
    }


def _verdict_note(page: Page) -> str:
    """One factual sentence per section, assembled from the counts alone."""
    verdict = page.verdict
    unverified = verdict.integrations_total - verdict.integrations_verified
    parts = [
        f"{verdict.integrations_verified} of "
        f"{_plural(verdict.integrations_total, 'integration point')} verified "
        f"against the live account"
        + (f", {unverified} awaiting an implemented test" if unverified else "")
        + "."
    ]
    if verdict.endpoints_total:
        parts.append(
            f"{_plural(verdict.endpoints_total, 'gateway route')} detected; "
            f"{verdict.endpoints_verified} verified."
        )
    if page.tools is None:
        parts.append("No agent tools declared.")
    else:
        parts.append(
            f"{verdict.tools_verified} of "
            f"{_plural(verdict.tools_total or 0, 'declared agent tool')} verified"
            + (
                f", {_plural(verdict.tool_changes_to_review, 'change')} waiting on a "
                "reviewer."
                if verdict.tool_changes_to_review
                else "."
            )
        )
    return " ".join(parts)


def _nav_block(page: Page) -> list[dict]:
    pill = {"VERIFIED": "ok", "AT RISK": "warn", "BLOCKED": "warn"}.get(
        page.verdict.word, "ok"
    )
    tools_count = (
        f"{page.verdict.tools_total} · {page.verdict.tool_changes_to_review}"
        if page.tools is not None
        else None
    )
    return [
        {"id": "posture", "label": "Security Posture", "pill": pill, "count": None},
        {
            "id": "tools",
            "label": "Agent tools",
            "pill": "soon" if page.tools is None else pill,
            "count": tools_count,
        },
        {
            "id": "api",
            "label": "API access",
            "pill": "soon" if not page.verdict.endpoints_total else pill,
            "count": page.verdict.endpoints_total or None,
        },
        {
            "id": "graph",
            "label": "Integrations",
            "pill": pill,
            "count": page.verdict.integrations_total or None,
        },
        {"id": "database", "label": "Database", "pill": "soon", "count": "soon"},
        {"id": "queue", "label": "Queue", "pill": "soon", "count": "soon"},
        {"id": "notanalyzed", "label": "Not analyzed", "pill": "soon", "count": None},
    ]


def _tool_band(page: Page) -> dict:
    if page.tools is None:
        return {
            "cls": "empty",
            "word": "No agent tools declared",
            "sub": (
                "Declare an MCP server's tools and this band reports them "
                "against every property the declaration claims."
            ),
            "review": "",
        }
    verdict = page.verdict
    word = {
        "VERIFIED": "TOOLS VERIFIED",
        "AT RISK": "TOOLS AT RISK",
        "BLOCKED": "TOOLS BLOCKED",
    }[verdict.word]
    return {
        "cls": VERDICT_CLASS.get(verdict.word, ""),
        "word": word,
        "sub": (
            f"<b>{verdict.tools_verified} of {verdict.tools_total}</b> tools "
            "verified against every declared property"
        ),
        "review": (
            _plural(verdict.tool_changes_to_review, "change") + " to review"
            if verdict.tool_changes_to_review
            else ""
        ),
    }


def _tools_blocks(page: Page) -> tuple[list[dict], dict]:
    """The grouped tool table and the section's labels and exceptions."""
    empty = {
        "eyebrow": "not declared in this run",
        "emptyTitle": "No agent tools declared",
        "emptyBody": (
            "This run carried no tool ledger, so there is nothing to report "
            "here. The section is named rather than hidden: an absent sweep "
            "and a clean sweep must never look alike."
        ),
        "attest": "",
        "bars": [],
        "exceptionsHead": "",
        "exceptions": [],
    }
    if page.tools is None:
        return [], empty

    ledger = page.tools
    entries = [(server, tool) for server in ledger.servers for tool in server.tools]
    grouped: dict[str, list] = {}
    for _server, tool in entries:
        key = "egress" if tool.egress else tool.mutation
        if key not in MUTATION_GROUP:
            key = tool.mutation if tool.mutation in MUTATION_GROUP else "read"
        grouped.setdefault(key, []).append(tool)

    groups: list[dict] = []
    for key in GROUP_ORDER:
        tools = grouped.get(key)
        if not tools:
            continue
        heading, css_suffix, class_label = MUTATION_GROUP[key]
        headers = sorted({c.trait for tool in tools for c in tool.checks})
        rows = []
        flagged = {"changed": 0, "held_out": 0, "fail": 0, "critical": 0}
        passed = 0
        for tool in tools:
            by_trait = {c.trait: c for c in tool.checks}
            cells = []
            for trait in headers:
                check = by_trait.get(trait)
                if check is None:
                    cells.append(None)
                    continue
                fallback = (check.status.upper(), "none")
                token, cls = CHECK_CELL.get(check.status, fallback)
                cells.append({"txt": token, "cls": cls})
                if check.status in flagged:
                    flagged[check.status] += 1
            statuses = {c.status for c in tool.checks}
            if statuses and statuses <= {"pass", "n/a", "not_verifiable"}:
                passed += 1
            rows.append(
                {
                    "n": tool.name,
                    "flag": bool(statuses & {"changed", "fail", "critical", "held_out"})
                    or tool.held_out,
                    "cells": cells,
                }
            )
        tags = [{"cls": "locked", "txt": f"{passed} verified"}] if passed else []
        for status, label in (
            ("changed", "changed"),
            ("held_out", "held out"),
            ("fail", "failing"),
            ("critical", "CRITICAL"),
        ):
            if flagged[status]:
                tags.append({"cls": "warn", "txt": f"{flagged[status]} {label}"})
        if not tags:
            tags = [{"cls": "mute", "txt": "no checks recorded"}]
        groups.append(
            {
                "g": heading,
                "cls": css_suffix,
                "clsLabel": class_label,
                "count": len(tools),
                "headers": headers,
                "rows": rows,
                "tags": tags,
            }
        )

    servers = ", ".join(s.server for s in ledger.servers)
    environments = sorted({s.environment for s in ledger.servers if s.environment})
    eyebrow = servers + (f" · {', '.join(environments)}" if environments else "")
    exceptions = []
    for server in ledger.servers:
        for item in server.exceptions:
            change = None
            for tool in server.tools:
                for check in tool.checks:
                    if tool.name == item.tool and check.change is not None:
                        change = check.change
            exceptions.append(
                {
                    "tag": item.kind.replace("_", " "),
                    "title": f"{item.tool}"
                    + (f" — {item.trait}" if item.trait else "")
                    + f" ({server.server})",
                    "body": item.message,
                    "diff": (
                        {"previous": change.previous, "current": change.current}
                        if change
                        else None
                    ),
                }
            )

    summary = ledger.servers[0].summary if len(ledger.servers) == 1 else None
    bars = [
        {"v": ledger.declared, "l": "declared", "cls": "good"},
        {"v": ledger.verified, "l": "verified", "cls": "good"},
    ]
    if summary is not None:
        bars.append({"v": summary.live, "l": "live"})
        if summary.undeclared:
            bars.append({"v": summary.undeclared, "l": "undeclared", "cls": "attn"})
        if summary.orphaned:
            bars.append({"v": summary.orphaned, "l": "orphaned", "cls": "attn"})
    if ledger.changed:
        bars.append({"v": ledger.changed, "l": "changed", "cls": "attn"})

    labels = {
        "eyebrow": eyebrow,
        "emptyTitle": "",
        "emptyBody": "",
        "attest": (
            "<span class='b'>Tool-boundary report.</span> Each row is a tool the "
            "declaration names, and each column a trait its checks recorded. "
            "Every cell is the status the check reported — nothing here is "
            "inferred from a tool's name or its mutation class."
        ),
        "bars": bars,
        "exceptionsHead": "Needs a human — named, not hidden",
        "exceptions": exceptions,
    }
    return groups, labels


def _sweep_blocks(page: Page) -> tuple[list[dict], dict]:
    api = page.api
    columns = (
        ["Unauthenticated", "Authenticated"] if api.probe_kind_known else ["Status"]
    )
    groups = []
    for group in api.groups:
        endpoints = []
        for endpoint in group.endpoints:
            if api.probe_kind_known:
                cells = [
                    {"txt": endpoint.unauth, "cls": "refused"}
                    if endpoint.unauth
                    else None,
                    {"txt": endpoint.auth, "cls": "ok"} if endpoint.auth else None,
                ]
            else:
                cells = [
                    {
                        "txt": endpoint.status,
                        "cls": STATUS_CLASS.get(endpoint.status, "none"),
                    }
                ]
            endpoints.append({"m": endpoint.method, "p": endpoint.path, "cells": cells})
        verified = sum(1 for e in group.endpoints if e.status == "PASS")
        tags = (
            [{"cls": "locked", "txt": f"{verified} verified"}]
            if verified
            else [{"cls": "mute", "txt": "no verified probe"}]
        )
        groups.append({"res": group.resource, "eps": endpoints, "tags": tags})

    exceptions = [
        {"tag": "held out", "title": name, "body": "", "diff": None}
        for name in api.held_out
    ] + [
        {"tag": "not routed", "title": name, "body": "", "diff": None}
        for name in api.not_routed
    ]

    labels = {
        "eyebrow": (
            f"{_plural(api.total, 'route')} detected"
            + (f" · {page.environment}" if page.environment else "")
        ),
        "columns": columns,
        "emptyTitle": "No API sweep in this run",
        "emptyBody": (
            "No API Gateway route was detected in the Terraform this run read, "
            "so there is no endpoint to sweep."
        ),
        "attest": (
            "<span class='b'>Access-control report.</span> Every row is a route "
            "ITest detected in Terraform, carrying the status verify recorded "
            "for it. Verify does not yet record which registered test was the "
            "anonymous probe, so this run shows one status column rather than "
            "an unauthenticated/authenticated pair holding the same value."
            if not api.probe_kind_known
            else "<span class='b'>Access-control report.</span> Each row carries "
            "the outcome of the anonymous probe and of the authenticated probe."
        ),
        "bars": [
            {"v": api.total, "l": "routes detected"},
            {"v": api.verified, "l": "verified", "cls": "good"},
        ],
        "exceptionsHead": "Deliberately not swept — named, not hidden",
        "exceptions": exceptions,
    }
    return groups, labels


def _graph_blocks(page: Page) -> tuple[list[dict], list[dict], dict]:
    points = [
        {
            "id": p.id,
            "src": p.src,
            "tgt": p.tgt,
            "tag": p.tag,
            "wild": p.wild,
            "ext": p.ext,
            "hub": p.hub,
            "isNew": p.is_new,
            "status": p.status,
        }
        for p in page.graph.points
    ]
    chain = [
        {
            "id": c.id,
            "from": c.source,
            "to": c.target,
            "tag": c.tag,
            "isNew": c.is_new,
            "status": c.status,
        }
        for c in page.graph.chain
    ]
    sources = sorted({p.src for p in page.graph.points})
    if len(sources) == 1:
        root_label = sources[0].split(".")[-1]
        root_sub = sources[0]
    else:
        root_label = _plural(len(sources), "source")
        root_sub = ""
    labels = {
        "eyebrow": (
            f"{_plural(len(points) + len(chain), 'edge')} · "
            "every edge carries the status verify recorded"
        ),
        "alt": (
            f"{_plural(len(points), 'resource')} reached by {root_label}"
            + (f", and {_plural(len(chain), 'downstream edge')}" if chain else "")
        ),
        "rootLabel": root_label,
        "rootSub": root_sub,
        "pointsTitle": "Grant reach — role to resource",
        "pointsCount": f"{sum(1 for p in points if p['status'] == 'PASS')} verified "
        f"of {len(points)}",
        "pointsCaption": "RESOURCES REACHED",
        "chainTitle": "Event flow — downstream chain",
        "chainCount": f"{sum(1 for c in chain if c['status'] == 'PASS')} verified "
        f"of {len(chain)}",
        "chainCaption": "DOWNSTREAM",
        "emptyTitle": "No graphable integration in this run",
        "emptyBody": (
            "No IAM or event edge was detected, so there is no reach diagram to draw."
        ),
    }
    return points, chain, labels


def _not_analyzed_block(page: Page) -> dict:
    if not page.not_analyzed:
        return {
            "present": False,
            "emptyTitle": "Not analyzed is not in this run",
            "emptyBody": (
                "The census of resources no detector models is produced by "
                "`itest plan`, not by verify, so this page cannot report it "
                "yet. It is named rather than dropped: an empty list and an "
                "unavailable one are different facts."
            ),
            "headline": "",
            "body": "",
            "chips": [],
        }
    total = sum(page.not_analyzed.values())
    return {
        "present": True,
        "emptyTitle": "",
        "emptyBody": "",
        "headline": f"{_plural(total, 'resource')} carry no integration "
        "semantics ITest models yet",
        "body": (
            "These are endpoints, not edges: nothing here is an unverified "
            "connection. They are counted and named, never silently skipped."
        ),
        "chips": [
            f"{rtype} ×{count}" for rtype, count in sorted(page.not_analyzed.items())
        ],
    }


def _footer_block(page: Page) -> list[dict]:
    footer = page.footer
    entries = []
    if footer.commit:
        entries.append({"k": "commit", "v": footer.commit})
    entries.append({"k": "run", "v": footer.run})
    if footer.account:
        entries.append({"k": "account", "v": footer.account})
    if footer.region:
        entries.append({"k": "region", "v": footer.region})
    entries.append({"k": "elapsed", "v": f"{footer.elapsed_s:.2f}s"})
    entries.append({"k": "generated", "v": footer.generated_at})
    return entries


# ---------------------------------------------------------------------------
# Assembly.
# ---------------------------------------------------------------------------


def build_blocks(page: Page) -> dict[str, object]:
    """The seven data blocks, keyed by marker name."""
    tools, tool_labels = _tools_blocks(page)
    sweep, api_labels = _sweep_blocks(page)
    points, chain, graph_labels = _graph_blocks(page)
    return {
        "PAGE": {
            "verdict": _verdict_block(page),
            "nav": _nav_block(page),
            "posture": {
                "infraMeta": _plural(page.verdict.integrations_total, "point"),
                "toolsMeta": (
                    tool_labels["eyebrow"] if page.tools is not None else "not declared"
                ),
                "since": f"since {page.since}" if page.since else "",
            },
            "toolBand": _tool_band(page),
            "tools": tool_labels,
            "api": api_labels,
            "graph": graph_labels,
            "notAnalyzed": _not_analyzed_block(page),
            "footer": _footer_block(page),
        },
        "TOOLS": tools,
        "TOOLPOSTURE": [_tile(t) for t in page.tool_tiles],
        "SWEEP": sweep,
        "POSTURE": [_tile(t) for t in page.posture.tiles()],
        "POINTS": points,
        "CHAIN": chain,
    }


def template_text() -> str:
    """The shipped template's source."""
    template = resources.files(TEMPLATE_PACKAGE).joinpath(TEMPLATE_RESOURCE)
    return template.read_text(encoding="utf-8")


def render(page: Page, template_path: Path | None = None) -> str:
    """Render ``page`` into the template and return the self-contained HTML."""
    if template_path is None:
        document = template_text()
    else:
        document = template_path.read_text(encoding="utf-8")
    for name, value in build_blocks(page).items():
        marker = MARKER.format(name=name)
        if marker not in document:
            raise ValueError(
                f"The template has no injection point for {name}: expected the "
                f"exact marker {marker!r}."
            )
        document = document.replace(marker, f"const {BLOCKS[name]} = {_js(value)};", 1)
    return document


def extract_blocks(document: str) -> dict[str, object]:
    """Read the emitted data blocks back out of a rendered page.

    The tests pin structure on this rather than on the HTML, so restyling the
    page cannot break a data test and a data change cannot slip past one.
    """
    by_constant = {v: k for k, v in BLOCKS.items()}
    out: dict[str, object] = {}
    for match in _BLOCK_RE.finditer(document):
        name = by_constant[match.group("name")]
        raw = match.group("json")
        # Undo the script-safety escaping before parsing.
        raw = (
            raw.replace("\\u003c", "<").replace("\\u003e", ">").replace("\\u0026", "&")
        )
        out[name] = json.loads(raw)
    return out
