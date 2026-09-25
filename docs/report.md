# `itest report --html` — the readiness page

One self-contained HTML file answering the two release-day questions: what did
this run actually verify, and what still needs a human. It is **rendered from
your data by your pipeline** — there is no sample content anywhere on the page.

```sh
itest report --html                          # runs verify, writes readiness.html
itest report --html --environment staging    # runs verify as `verify --environment staging`
itest report --html --out release.html       # --out is the path flag (as on `redact`)
itest report --from verify.json              # render a run you already have
itest report --since .itest/prev-manifest.yaml   # adds trends and the since-line
itest report --redact                        # pseudonymized account ids
```

`--environment` is passed to the verify the report runs, with the same policy
and binding resolution and the same refusals as `itest verify --environment`:
an environment the policy does not define, or a production one that lists the
active tier, exits 2 and writes nothing. It cannot be combined with `--from`,
whose document already records where it ran. `--output PATH` is a deprecated
alias for `--out` for one release (on `plan` and `verify`, `--output` is a
format), and warns.

Exit code is 0 whenever the page renders. **The verdict never becomes an exit
code** — that is `itest verify`'s job, and a report command that failed the
build would make people stop running it.

## Two audiences, two layers, one set of data

The page opens with the **Answer**: a plain-language block for whoever has
to decide whether to ship, readable in five seconds with none of the method
vocabulary. Below a divider labelled **Detail** is everything the page showed
before it existed — the banner, the posture grids, the tools grid with its
lifecycle tags, the standards band, the evidence lanes, the sweep, the graph
and the footer — unchanged, for the engineer who needs the exact terms (held
out, not verifiable, stale, external evidence, safe floor, integration point).
Both layers are rendered from the same verify document and manifest; the
Answer adds no data, only words.

### The Answer block

Top to bottom:

1. **The verdict word**, large, in its band colour (see Verdict below).
2. **One sentence**, chosen by the word and nothing else — written by
   `derive_verdict` alongside the word, so the template never re-derives
   meaning from counts:

   | Word | Sentence |
   | --- | --- |
   | BLOCKED | `{n} finding(s) need attention before release.` |
   | NEEDS REVIEW | `No findings. {n} tool change(s) are waiting for a reviewer.` |
   | PARTIAL | `No findings. {passed} of {total} checks passed; {held} were not run here.` |
   | VERIFIED | `No findings. All {total} checks passed.` |

   A *check* is one cell of the tools grid — the same unit it counts — plus
   one integration point that is not itself a declared tool. `{held}` is the
   held-out, not-verifiable and not-run cells and the stub or gated points.
3. **Findings**, only when there are any: one line per critical or failing
   check — severity, tool, the trait's human name from the trait table
   (`destructive gating`, never the slug) and the check's recorded detail —
   critical first; then failing or errored points. When the list is empty the
   sentence already said "No findings" and no heading is drawn.
4. **What was checked here**: `Ran: {families} — {passed} of {ran} passed.`
   and one `Not run here: {families} — {why}.` line per reason. Families are
   the human names (Authority, Blast radius, Containment, Change; Integrations
   for Terraform points). `{why}` is one of exactly three phrases:

   | Page state | Phrase |
   | --- | --- |
   | a check held out by the environment policy, a gated point (the safe floor, or an environment that withholds the tier) | `these need a staging environment with credentials` |
   | `not_verifiable` because the server's declaration lacks a fact (the missing fact is named when the check recorded it) | `this server does not declare what they need` |
   | `not_verifiable` because no engine check exists for the trait yet, a registered check that did not run, a stub point | `the check exists but has not been written yet` |
5. **Red team**, only when a source was read, one line per source in one of
   two templates, chosen by whether the harness declared what it was aiming
   at (`testCase.metadata.target_tool`, see [docs/evidence.md](evidence.md)):

   - targeting declared: `Red team ({date}): {rows} attempts. {t} targeted
     {tool}; {i} induced a call; {r} of those were refused by the tool.` —
     `t` rows named the tool as their target, `i` rows called it, `r` of
     those calls the tool refused. The refused clause appears only when
     `i > 0`.
   - targeting not declared: `Red team ({date}): {rows} attempts. {m} tool(s)
     exercised. No write or destructive tool was called.` — the last clause
     is computed by joining each called tool to its mutation class; if any
     called tool is write or destructive it becomes `{k} write/destructive
     tool(s) were called: {names}`, styled as a warning. Never "on purpose",
     never "resisted": without declared targeting ITest does not know intent.

   Stale evidence appends `(stale — from {date})`. Neither template is a
   verdict input: the word is identical with and without evidence, and
   `tests/test_evidence_never_counts.py` pins that.
6. `Detail below.`

### The jargon contract

The block uses none of the detail layer's vocabulary, and a test
(`tests/test_answer.py`) renders the CI fixture and asserts it: no "held
out", "not verifiable", "environment bound", "safe floor", "integration
point", "judgment", "lane", "harness", "targeting", "tier", "readonly",
"stub", "orphan", "manifest", "lifecycle", "rollup", "evidence lane",
"external evidence" or "declared"; no trait slug, standards id, run id or
ISO timestamp. Every label in the block is data, so the template's script
carries no words of its own. The detail layer keeps every one of those
terms, spelled exactly as before.

On the safe floor (a policy present, nothing bound) the banner's subtitle
reads `Ran in CI without credentials — active checks not run`; the exact
original string (`no environment bound — N integration points · safe floor
(static, readonly)`) is the footer's `bound` entry. `itest report` prints
the same Answer to the terminal after `Wrote …`, which is what the scheduled
red-team job's summary step shows on the run page.

## The rule the whole page obeys

Every number, name and status comes from one of exactly three places: the
document `itest verify --output json` emits, `.itest/manifest.yaml`, or — only
with `--since` — a prior manifest. Everything else on the page is a fixed
label.

There is no illustrative element. When a source is absent the section renders
**empty but named** ("No agent tools declared", "No API sweep in this run"),
never as plausible-looking sample data. An absent sweep and a clean sweep must
never look alike.

## Verdict

The word in the stamp, in the order the rules are tried:

| Verdict | When |
| --- | --- |
| **BLOCKED** | a finding: any tool check is `critical` or `fail` (or a server's `summary.critical` is nonzero), or any point is `failing` or `error`. Red. |
| **NEEDS REVIEW** | a human decision is pending: any tool check is `changed` and unreviewed, or `stale` (hand-edited against a schema the tool no longer has), or a point reports `stub` while its manifest entry says `implemented`. Amber. |
| **PARTIAL** | nothing failed and nothing is pending, but the page cannot claim full coverage: checks were `held_out` by the environment policy, are `not_verifiable` for this server, or are not written yet; a declared tool is not verified; or a point reports `stub` or `gated`. Nothing is wrong that we know of; we did not look at everything. Grey-blue, never amber, never green. |
| **VERIFIED** | every declared property of every declared tool passed, every point verified, and no tool change waiting on review. Green. |

PARTIAL is the word that matters most in practice: **a stub is not coverage,
and unchecked is not danger.** A freshly synced project has a manifest full of
stubs and a green pytest run, and a page that stamped VERIFIED over "0 of 26
verified" would be the most misleading thing ITest could print — but a page
that stamped a risk word over it would be the second most misleading, because
nothing was found. The four words are derived in one place,
`itest.report.model.derive_verdict`, together with the plain sentence the page
shows beneath the word.

Point statuses are verify's own, under its existing precedence
(fail > error > pass > stub), so the page never re-derives a status the
verifier already decided.

## Section by section

| Section | What it shows | Source |
| --- | --- | --- |
| Answer | the verdict word and sentence, findings, ran / not run by family, the red-team line | verify JSON `tools` and `points[]`, the trait table for human names, the manifest's `evidence` and `sources` |
| Posture — integrations verified (tile) | `passing` / `total_points` | verify JSON |
| Posture — agent tools verified (tile) | `tools.servers[].summary.verified` / `.declared` | verify JSON `tools` |
| Verdict — endpoints verified | route_edge points passing / detected | verify JSON `points[]` + manifest `type` |
| Verdict — drift | `orphaned_tests` + `len(unregistered)` | verify JSON |
| Verdict — since-line | new / removed point ids | manifest vs `--since` manifest |
| Posture — cross-stack | points with `attributes.external` | verify JSON `points[].attributes` |
| Posture — wildcard | points with `attributes.wildcard_resource` | verify JSON `points[].attributes` |
| Posture — broad managed | points with `attributes.broad_managed_policy` | verify JSON `points[].attributes` |
| Posture — unauthenticated routes | route_edge points with `attributes.auth == "NONE"` | verify JSON + manifest `type` |
| Posture — trend arrows | the same four counts over the prior manifest | `--since` only |
| Agent tools — band, tiles | `tools.servers[].summary`, `.families[]` | verify JSON `tools` |
| Agent tools — rows | one row per `tools[]`, one column per trait in `checks[]` | verify JSON `tools` |
| Agent tools — exceptions | `tools.servers[].exceptions[]`, with the check's `change` diff | verify JSON `tools` |
| Standards band | `tools.standards[]` — one row per OWASP Agentic entry (id, title, coverage, status counts, the reason when not covered); derived from the checks when an older document lacks it | verify JSON `tools`, `itest/traits/standards.yaml` for titles |
| API sweep — rows | `attributes.method`, `attributes.path` per route_edge point | verify JSON + manifest `type` |
| API sweep — status cell | the point's status, as `PASS`/`FAIL`/`ERROR`/`STUB`/`GATED` | verify JSON `points[].status` |
| Integration graph | iam_edge points (nodes) and event_edge points (chain) | verify JSON + manifest `type` |
| Coverage list — tag | `points[].tag`, the same one-line tag `itest plan` prints | verify JSON |
| Coverage list — evidence id | `points[].id` | verify JSON |
| Not analyzed | the plan's census of unmodelled resource types | **not available** — see below |
| Footer — run | `environment`, plus `--redact` when passed | verify JSON |
| Footer — bound | on the safe floor only: `no environment bound — N integration points · safe floor (static, readonly)` | verify JSON `on_safe_floor` |
| Footer — account, region | read out of the ARNs the report's own strings carry | verify JSON |
| Footer — elapsed | `elapsed_seconds` | verify JSON |

### A declarations-only project

The Infrastructure tiles, the API access sweep, the Integration graph and Not
analyzed are Terraform-side sections; for a declarations-only project (tools
declared, no Terraform read — `examples/reference-mcp`,
`examples/terraform-mcp-server`) they are not rendered, nor are their nav
entries, and the detail layer's footer carries one line instead:
`No Terraform in this project; infrastructure sections are not shown.`
Nothing records whether a run read Terraform, so the page **derives** it
conservatively —
`itest.report.model.is_declarations_only`: a tool ledger is present and there
is no integration point that is not a declared tool, no route, no graph edge
and no not-analyzed census — and a project with Terraform renders every
section exactly as before.

## Roadmap

The page shows what this project has and advertises nothing: there is no
card for a probe that has not run. Two probes are designed and not yet built,
and this is where that is written down rather than on a release page:

- **Database — read/write round-trip.** Designed on the same probe framework
  as the API sweep, and gated by the same environments policy. Comes online
  once a project answers one question: which CRUD operations may a probe
  perform, and against which table?
- **Queue → function — marked-message delivery.** Event-source mappings and
  dead-letter redrives are verified as wiring today (see the integration
  graph). The active probe would send a marked, self-cleaning message and
  observe it arrive — proving delivery, not just configuration. Opt-in per
  environment; refuses production.

Both are on the scope ledger in [DESIGN.md](../DESIGN.md) under "Not yet
built".

## What verify cannot supply today

These render as named empty states rather than as guesses. Each is a
follow-up, not a defect in the page:

The agent-tool ledger is **not** among them. `itest verify --output json`
emits `tools.servers[]` whenever the manifest records a declared tool
(`verifier.build_tool_ledger`), in exactly the shape pinned by
`tests/fixtures/report/tool-ledger.json` and its contract test, each check
carrying its lifecycle `state`. Only a run whose manifest declares no tool omits
the key, and the section then reads "No agent tools declared".

| Field | Why it is absent | Unblocked by |
| --- | --- | --- |
| `not_analyzed` | the census is computed by `itest plan` and written to `.itest/plan.json`; verify never sees it | a `not_analyzed` block in verify JSON |
| `verdict.endpoints_refuse_anon`, `verdict.authenticated_200` | verify records a point's status but never **which registered test was the anonymous probe**; the http_probe recipe suggests name suffixes in prose but nothing freezes or closes that vocabulary, so deriving a probe kind from a function name would be a guess | a `probe_kind` field on `tests[]` in verify JSON |
| `ApiEndpoint.unauth` / `.auth` | same cause | same |
| `footer.commit` | not in verify JSON or the manifest | a `--commit` flag, or a run block in verify JSON |

While the probe kind is unknown the API sweep renders **one Status column**,
never an Unauthenticated/Authenticated pair holding the same neutral value in
both cells. The pair returns automatically the day verify records which probe
was which — the model already carries both fields.

## `--since` and trends

Trend arrows, the "since" line in the verdict banner and the `new` marks on
graph rows appear **only** when `--since <prior manifest>` is given. Without it
they are omitted entirely — not rendered as "steady", not rendered as an em
dash. A page that shows a flat arrow when nothing was compared is claiming a
comparison it never made.

## `--redact`

Runs verify's own document scrubber (`itest.core.redact.text_scrubber`) — the
same one `verify --redact` uses, over the same fields. Account ids and
high-entropy credential patterns are pseudonymized in every string on the page,
including the footer and the graph's node labels. It does not strip
human-readable resource names; that is the documented scope of `itest redact`.

## How the page is built

`itest/report/model.py` turns the verify document and the manifest into the
page's data and decides nothing about markup. `itest/report/render.py` injects
that data into `itest/report/templates/readiness.html` at seven exact markers
(`<!-- itest:data:PAGE -->` and one per JS data block) and decides nothing
about meaning. No templating language, no new runtime dependency.

Tests pin **the data blocks, not the pixels** (`tests/fixtures/report/snapshots/`),
so restyling the page cannot break a data test and a data change cannot slip
past one.
