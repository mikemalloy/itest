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
| **BLOCKED** | any tool check is `critical` (or a server's `summary.critical` is nonzero), or any point is `failing` or `error` |
| **AT RISK** | any tool check is `changed` and unreviewed; or a point reports `stub` while its manifest entry says `implemented`; or any point reports `stub` at all |
| **VERIFIED** | every point verified, and no tool change waiting on review |

The last AT RISK clause is the one that matters most in practice: **a stub is
not coverage.** A freshly synced project has a manifest full of stubs and a
green pytest run, and a page that stamped VERIFIED over "0 of 26 verified"
would be the most misleading thing ITest could print.

Point statuses are verify's own, under its existing precedence
(fail > error > pass > stub), so the page never re-derives a status the
verifier already decided.

## Section by section

| Section | What it shows | Source |
| --- | --- | --- |
| Verdict — integrations verified | `passing` / `total_points` | verify JSON |
| Verdict — endpoints verified | route_edge points passing / detected | verify JSON `points[]` + manifest `type` |
| Verdict — drift | `orphaned_tests` + `len(unregistered)` | verify JSON |
| Verdict — agent tools verified | `tools.servers[].summary.verified` / `.declared` | verify JSON `tools` |
| Verdict — since-line | new / removed point ids | manifest vs `--since` manifest |
| Posture — cross-stack | points with `attributes.external` | verify JSON `points[].attributes` |
| Posture — wildcard | points with `attributes.wildcard_resource` | verify JSON `points[].attributes` |
| Posture — broad managed | points with `attributes.broad_managed_policy` | verify JSON `points[].attributes` |
| Posture — unauthenticated routes | route_edge points with `attributes.auth == "NONE"` | verify JSON + manifest `type` |
| Posture — trend arrows | the same four counts over the prior manifest | `--since` only |
| Agent tools — band, tiles | `tools.servers[].summary`, `.families[]` | verify JSON `tools` |
| Agent tools — rows | one row per `tools[]`, one column per trait in `checks[]` | verify JSON `tools` |
| Agent tools — exceptions | `tools.servers[].exceptions[]`, with the check's `change` diff | verify JSON `tools` |
| API sweep — rows | `attributes.method`, `attributes.path` per route_edge point | verify JSON + manifest `type` |
| API sweep — status cell | the point's status, as `PASS`/`FAIL`/`ERROR`/`STUB`/`GATED` | verify JSON `points[].status` |
| Integration graph | iam_edge points (nodes) and event_edge points (chain) | verify JSON + manifest `type` |
| Coverage list — tag | `points[].tag`, the same one-line tag `itest plan` prints | verify JSON |
| Coverage list — evidence id | `points[].id` | verify JSON |
| Not analyzed | the plan's census of unmodelled resource types | **not available** — see below |
| Footer — run | `environment`, plus `--redact` when passed | verify JSON |
| Footer — account, region | read out of the ARNs the report's own strings carry | verify JSON |
| Footer — elapsed | `elapsed_seconds` | verify JSON |

The **Database** and **Queue** cards are labelled product tiers, not data. They
say "Designed · not yet built" and carry no numbers, because those probes are
on the roadmap and nothing has run.

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
