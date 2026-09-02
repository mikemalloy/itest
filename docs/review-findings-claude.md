# ITest review — Claude's pass (commit 064e361)

Reviewed against the six invariants in `review-brief.md`. Findings ranked by
severity; each marked **[reproduced]** (I ran it and saw the failure),
**[code-confirmed]** (verified by reading the exact lines), or **[reported]**
(a subagent found it with a plausible scenario; I did not independently run it).
Fix sketch is a direction, not a spec.

## Correction to the brief (own it first)

Invariant 5 in the brief says redaction covers "account ids **(and bucket
names)**." That was my overclaim — the README only ever promised account-id
redaction, and the code matches the README. So "bucket names aren't redacted"
is **not a bug against contract**; it's a product question (finding C1). Tell
Antigravity to judge redaction against *account ids only*.

## Findings

**F1 — HIGH — [reproduced] — `verify --redact` scrubs only account ids, but the
output implies full safety.** `verifier.py:356-364` applies only
`account_pseudonymizer()` (regex `\b\d{12}\b`) over the report. `TestResult.detail`
carries pytest assertion messages and collection tracebacks verbatim
(`render_human` prints them; json/junit dump them). A failing check whose
assertion message contains a connection string, token, or response body emits it
in full — while the human-mode line "Tip: use `--redact` before sharing this
output" (`verifier.py:457-461`) tells the user it's now safe to share. *Fix:*
run the full `redact_document`-class scrubbing (high-entropy tokens too), not
just accounts, over report detail before printing; soften the Tip.

**F2 — MEDIUM — [reproduced] — a `disabled` manifest entry still executes.**
`_gating_args` (`verifier.py:135`) `continue`s past disabled tests, and nothing
else removes them from the pytest run, so the function is collected and runs. I
appended a `disabled: true` entry with a side effect and `itest verify` fired it
(`RAN_MARKER` written). Honest reachability: **no CLI sets `disabled` today**, so
it needs a hand-edited manifest — but the manifest is a documented human-editable
artifact and `disabled_reason` invites exactly this, and an `active` test
disabled *because it mutates* would run even bound to prod. *Fix:* add disabled
tests' canonicals to the `--ignore`/`--deselect` set alongside gated ones.

**F3 — MEDIUM — [code-confirmed] — the SG detector silently drops IPv6,
prefix-list, and self ingress.** `sg_edges.py:142,178` read only `cidr_blocks`
and `security_groups`; there is no `ipv6_cidr_blocks`, `prefix_list_ids`, or
`self` anywhere in the file (grep: 0). An ingress rule exposing a port to
`ipv6_cidr_blocks = ["::/0"]` with empty `cidr_blocks` emits **zero** edges — a
publicly reachable entry point is invisible, in the detector whose whole job is
reachability. Uncovered by fixtures/tests. *Fix:* add the three source kinds;
`self`→the SG's own address, `prefix_list_ids`→external tagged.

**F4 — MEDIUM — [reproduced] — the HTTP probe accepts any URL scheme and host.**
`http.py` keeps urllib's default `FileHandler`/`FTPHandler`; `probe("file:///…")`
opens the file (returned status None, no error). Route/API base URLs come from
untrusted Terraform state, so a crafted state points an active probe at
`file:///etc/passwd` or `http://169.254.169.254/…` (metadata). *Fix:* enforce
`{http,https}`; optionally block link-local/loopback/metadata IPs.

**F5 — MEDIUM — [reported] — concurrent `verify` runs corrupt each other.**
`_run_pytest` unlinks/rewrites a single fixed `.itest/_verify_report.json`, and
`save_manifest` (`manifest.py:160-166`) is a plain truncate-write. Two runs in
one checkout race — one deletes the other's report mid-collection (results come
back empty→all "stub"), and a concurrent duration-persist can tear the YAML.
*Fix:* per-run temp report file; atomic write+rename for the manifest.

**F6 — LOW/MEDIUM — [reproduced] — a malformed environment spec crashes instead
of erroring cleanly.** `environments.py:147` guards only falsy specs; `dev:
readonly` (a string instead of `{tiers: […]}`) reaches `spec.get(...)` and raises
`AttributeError`, not the promised `EnvironmentConfigError` — so it escapes
`cli.py`'s handler as an unhandled traceback with the wrong exit code. *Fix:*
type-check each spec is a mapping at load, raise `EnvironmentConfigError`.

**F7 — LOW — [code-confirmed] — `lambda_permission` id omits `action`.**
`event_edges.py` folds source/target/mechanism/qualifier but not `action`; two
grants from one principal (e.g. `InvokeFunction` vs `InvokeFunctionUrl`, no
`source_arn`, same empty qualifier) get identical ids and the second is dropped
by `emit()`. *Fix:* add `action` to the permission discriminator.

**F8 — LOW — [reported] — `eventbridge_target` discriminator is `bus=` only.**
Two targets on one rule to the same destination differing only by
`input`/`input_transformer` collapse to one point. *Fix:* fold a hash of the
input transform into the discriminator (never `target_id` — that's generated).

**F9 — LOW — [reported] — non-UTF8 `--tf-json` raises uncaught
`UnicodeDecodeError`** instead of `PlanInputError` (`planner.py` catches only
`FileNotFoundError`/`JSONDecodeError`). *Fix:* catch and wrap.

**F10 — LOW — [code-confirmed] — no subprocess timeout.** Neither the pytest run
(`verifier.py:186`) nor `terraform show` (`planner.py:103`) sets `timeout=`; a
hung test or a backend-locked terraform hangs the CLI forever. *Fix:* a generous
timeout with a clear message.

**F11 — LOW — [reported] — the fake-account generator can collide with a real
leading-zero account.** `redact.py` seeds `_used` only with fakes it issues, so a
real `000000000010` and a generated pseudonym can coincide, mis-correlating two
entities in "sanitized" output. *Fix:* pre-seed `_used` with all 12-digit ids
found in the document.

**F12 — LOW — [reported] — a new stub can shadow an unregistered human function
of the same name.** `syncer.py:268` builds `used_names` from the manifest only,
not the file's actual defs; append-only preserves the human's bytes but Python
binds the name to the last `def`, silently shadowing theirs. *Fix:* also scan the
file's real function names (AST) when picking a non-colliding stub name.

## Product question, not a bug

**C1 — resource names (bucket, cluster, secret names) are not redacted.**
`redact` pseudonymizes account ids and high-entropy tokens, but a low-entropy
human-readable name like `acme-prod-customer-exports` passes through (reproduced).
This matches the README's account-id-only contract, so it's not a defect — but a
user sharing `itest plan` output or a diagram leaks company/data-classification
signal, and F1's "safe to share" Tip makes that worse. *Decide:* is `--redact`
"account ids only" or "safe to share"? If the latter, add opt-in resource-name
pseudonymization; either way align the docs and the Tip.

## Clean bills (invariants I confirmed hold)

- **Invariant 1 (id stability):** upheld across all five detectors — no hash
  folds an index, a generated name, or an apply-rewritten value; resolution
  guards same-name collisions. Thorough trace, [code-confirmed].
- **Invariant 2 (sync append-only):** sound — no path rewrites/truncates a test
  file; ownership mismatch only bumps a counter; orphans flagged never deleted.
  (F12 is the one narrow corner.)
- **Invariant 3 (rollup fail>error>pass>stub; collection error ≠ stub):** sound,
  [code-confirmed] at `verifier.py:309-320`.
- **Invariant 4 (read-only; tier gating):** tier dimension sound — load-time
  refusal of prod+active, collection-time `--ignore`/`--deselect`, safe floor on
  absence. The `disabled` dimension (F2) is the exception.
- **Invariant 6 (strip_ansi(style(x))==x):** sound; soft_wrap/markup-off verified.
- **Also confirmed:** manifest round-trip is field-stable and order-preserving;
  `itest add` is human-owned-from-birth; subprocess calls are list-form and
  injection-safe with `--rootdir` pinned; all untrusted YAML uses `safe_load`;
  account-id redaction is complete and idempotent past 10 accounts.

## Reconciliation plan

When Antigravity's list is back: match on `file:line`. Agreements → confirmed,
fix first (expect F1–F4 to appear in both). Only-me or only-Antigravity → I
re-verify before it counts. Semgrep (`.semgrep/itest.yml`, pinned, offline) is
clean; it's committed as the CI security gate. Fixes land as one numbered
prompt, tests-first, most-severe first.
