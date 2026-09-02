# ITest dual code review — reconciled findings (commit 064e361)

Two independent reviews (Claude + Antigravity) from the same brief, reconciled
by matching on `file:line` and **empirically re-verifying every disputed or
single-source finding before it counts**. That verification changed the
outcome: it promoted a Critical one reviewer missed, and it killed two HIGHs the
other reviewer reported that turned out to be false. Severity is post-verification.

## What the dual review caught that a single pass would not

- Antigravity found a **Critical** (F0) that Claude's pass missed.
- Claude found F1/F2/F3 that Antigravity's pass missed.
- Both independently found the probe scheme issue (F5) — high confidence.
- Claude's empirical re-verification caught **two of Antigravity's HIGH findings
  as false positives** (redact idempotence, async classification) — two fixes
  avoided.

## Confirmed findings (ranked, all re-verified)

**F0 — CRITICAL — [reproduced] — `verify` hardcodes the pytest target to
`itest_tests/`, so an `itest add`-registered test living anywhere else never
runs and rolls up as a passing-looking STUB.** `verifier.py:164` runs `pytest
itest_tests`. Register `tests/integration/test_alb.py::test_alb_health` (the
whole Ring-3 adoption use case `itest add` exists for) → the file is never
collected → `outcomes.get(...)` is None → classified `missing` → point shows
`[STUB]`, verify exits 0. A test the user believes is guarding an integration
silently does not run. *(Antigravity; Claude missed.)* *Fix:* run pytest over
the set of directories the manifest's registered paths actually live in (or the
project root with the manifest paths as node-id filters), not a fixed dir.

**F1 — HIGH — [reproduced] — `verify --redact` scrubs only account ids, but tells
the user the output is safe to share.** `verifier.py:356-364` applies only
`account_pseudonymizer()`; `TestResult.detail` (assertion messages, tracebacks)
prints verbatim in human/json/junit, and the human "Tip: use `--redact` before
sharing" (`:457-461`) implies safety. A failing check whose message contains a
token or connection string leaks it under `--redact`. *(Claude.)* *Fix:* full
token-scrubbing over report detail; soften the Tip.

**F2 — MEDIUM — [reproduced] — a `disabled` manifest entry still executes.**
`verifier.py:135` skips disabled tests when computing gating args, and nothing
else removes them from collection, so the function runs (verified: side-effect
fired). No CLI sets `disabled` today (needs a hand-edited manifest), but
`disabled_reason` documents the feature, and an `active` test disabled *because
it mutates* would run even in prod. *(Claude.)* *Fix:* add disabled canonicals
to the `--ignore`/`--deselect` set.

**F3 — MEDIUM — [code-confirmed] — the SG detector drops IPv6, prefix-list, and
self ingress.** `sg_edges.py:142,178` read only `cidr_blocks` + `security_groups`
(grep: 0 hits for ipv6/prefix_list/self). An `ipv6_cidr_blocks=["::/0"]` ingress
emits zero edges — a public entry point invisible in the reachability detector.
*(Claude.)* *Fix:* add the three source kinds.

**F4 — MEDIUM — [reproduced] — the HTTP probe accepts any URL scheme and host.**
`http.py` keeps urllib's default `FileHandler`/`FTPHandler`; `probe("file:///…")`
reads the file. Base URLs come from untrusted state → `file:///etc/passwd`,
`http://169.254.169.254/…`. *(Both reviewers — F4≡AG#5, high confidence.)* *Fix:*
enforce `{http,https}`; block link-local/metadata.

**F5 — MEDIUM — [reported] — concurrent `verify` runs corrupt each other** via a
fixed `.itest/_verify_report.json` and a non-atomic `save_manifest`. *(Claude.)*
*Fix:* per-run temp report; atomic write+rename.

**F6 — LOW/MEDIUM — [reproduced] — a malformed environment spec crashes** with
`AttributeError` instead of the promised `EnvironmentConfigError` (`dev: readonly`
→ `spec.get` on a str). *(Claude.)* *Fix:* type-check each spec is a mapping at
load.

**F7 — LOW/MEDIUM — [reproduced] — `itest plan` on plan-JSON crashes on a null
`security_group_id`.** A `terraform plan -json` where the id is known-after-apply
→ `_point_id` does `"|".join([... None ...])` → `TypeError`. *(Antigravity #7.)*
The README says "state beats plan," but plan should degrade, not crash. *Fix:*
skip/flag rules with unresolved required refs.

**F8 — LOW/MEDIUM — [reproduced] — an IAM grant on `arn:aws:s3:::bucket/*` is
marked external though the bucket is in-stack.** `iam_edges` indexes the bucket
ARN exactly, so the object-space `/*` ARN doesn't match and resolves external.
Defensible (it *is* a different ARN) but unhelpful — the reader sees "external"
with the bucket right there. *(Antigravity #4.)* *Fix:* resolve `bucket/*` to the
bucket resource, tagged as object-scope.

**F9 — LOW — [reproduced] — the junit summary line omits the `errored` count.**
`cli.render_verify_line` prints passing/failing/stubs/orphaned/gated but not
errored, so `verify --output junit` with a broken import shows no error while
exiting 2. (Human mode does show it.) *(Antigravity #6.)*

**F10 — LOW — [code-confirmed] — `lambda_permission` id omits `action`;** two
grants from one principal (InvokeFunction vs InvokeFunctionUrl, no source_arn)
collapse to one point. *(Claude.)*

**F11 — LOW — [reported] — `eventbridge_target` discriminator is `bus=` only;**
two targets on one rule to one destination with different input collapse.
*(Claude.)*

**F12 — LOW — [reported] — non-UTF8 `--tf-json` raises uncaught
`UnicodeDecodeError`** not `PlanInputError`. *(Claude.)*

**F13 — LOW — [code-confirmed] — no subprocess timeout** on pytest or terraform;
a hang blocks the CLI forever. *(Claude.)*

**F14 — LOW/NIT — [code-confirmed] — `_status_from_body` is string-matching, not
AST.** Class-nested methods and same-named unregistered functions can misclassify
or shadow. *(Claude F12 + Antigravity #9.)* *Fix (both reviewers agree):* reuse
the `ast` parse that `register.py` already does — this dissolves F14 and hardens
classification generally.

## False positives — verified and NOT fixing (verification earning its keep)

- **AG#2 (claimed HIGH: redact idempotence breaks at ≥10 accounts) — REJECTED.**
  The analysis correctly saw `_is_pseudonym("000000000010") → False` but missed
  the `while fake not in self._used` collision-avoidance loop at
  `redact.py:204-208`, which deterministically re-lands the 10th account on
  `000000000010`. Verified: re-redacting a 12-account doc is byte-identical, and
  `redact --check` returns 0 findings on it.
- **AG#3 (claimed HIGH: `async def` tests misclassified as stub) — REJECTED.**
  `text.find("def name(")` matches `async def name(` as a substring. Verified:
  async implemented → `implemented`, async stub → `stub`, both correct.
- **Claude's F11-fake-account-collision (from a sub-agent) — WITHDRAWN.** The same
  `_used` collision loop defends against it.

## Not a bug — a product/doc decision

- **C1 — resource names (bucket/cluster/secret) are not redacted.** Reproduced,
  but this matches the README's account-id-only contract — Claude's *brief*
  overclaimed by listing "bucket names" under invariant 5. Combined with F1's
  "safe to share" Tip, decide whether `--redact` means "account ids only" or
  "shareable," and align docs + Tip either way.

## Invariants — reconciled clean bills (both reviewers agree)

1 (id stability), 2 (sync append-only, modulo F14), 3 (rollup precedence —
correct in isolation; F0 is the runner-scope hole around it), 4 (tier gating;
F2 `disabled` is the exception), 6 (colorizer). Both also confirm: subprocess
calls are list-form and injection-safe with `--rootdir` pinned; all untrusted
YAML is `safe_load`; `itest add` validates via AST without importing; account-id
redaction is complete and idempotent.

## Fix plan

One numbered prompt, tests-first, most-severe first: **F0 → F1 → F2 → F4** in the
first pass (the Critical, the leak, the two gating/robustness holes), then a
sweep of the LOWs with the shared **`_status_from_body` → AST** refactor folding
in F14. Semgrep (`.semgrep/itest.yml`, pinned, offline-clean) becomes the CI
security gate. Each fix starts with a failing regression test that encodes the
scenario above.
