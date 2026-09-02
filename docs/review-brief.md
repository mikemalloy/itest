# ITest code review brief (for independent reviewers)

You are doing a senior-architect review of ITest at commit `064e361`. Another
reviewer is reviewing the same code from this same brief; your findings will be
reconciled against theirs, so be specific and cite `file:line`.

## What ITest is (so you judge it against its own contract)

A local-first CLI that reads `terraform show -json`, detects integration points
(security-group / IAM / event / API-route / load-balancer edges), writes pytest
stubs + a YAML manifest, and runs read-only checks against a live AWS account,
reporting coverage per integration point. ~5,000 lines of source, ~7,700 of
tests. Engine is a library; the CLI and (future) MCP server are callers.

## The load-bearing invariants — a violation of any of these is a top finding

1. **Point IDs are content-derived and stable.** An edge's id hashes its
   semantic content (type, endpoints, discriminators), never array position or
   a Terraform-generated name. Re-running on unchanged infra must yield
   identical ids. Look in `itest/core/detectors/*` and `points.py`. A hash that
   folds in a value Terraform rewrites at apply (a generated suffix, a target_id,
   a list index) is a bug.
2. **Sync never rewrites human-touched work.** `syncer.py` records an ownership
   hash per generated file and, on mismatch, appends but never rewrites or
   deletes a function a human edited. Tests registered via `itest add` are
   human-owned from birth. Orphaned points are flagged, never deleted.
3. **Verify rollup precedence is fail > error > pass > stub**, and a collection
   error is never mistaken for a stub. One broken test module must not blind the
   rest. `verifier.py`.
4. **Read-only by default; active tier is gated.** Nothing mutates AWS. Active
   tests never run without both a committed policy allowing the tier AND a
   binding to an environment that allows it; a production environment refuses
   the active tier at load time. `environments.py`, `verifier.py` gating.
5. **Redaction is consistent and complete.** `--redact` pseudonymizes account
   ids (and bucket names) with stable pseudonyms preserving referential
   integrity, and is idempotent. Nothing unredacted leaks in any output mode.
   `redact.py`.
6. **Presentation is a colorizer, never a rewrite.** `strip_ansi(style(x)) == x`
   for all output; piped/CI output is byte-identical to plain. `style.py`.

## Dimensions to review, report findings under each

- **Correctness** — logic errors, wrong precedence, resolution that picks the
  wrong resource, edge cases in the detectors (weighted/blue-green LB, multi-hop
  events, module-nested addresses, wildcard IAM).
- **Security** — subprocess construction, YAML/JSON parsing of untrusted state,
  the HTTP probe (SSRF surface, redirects, timeouts), path handling, redaction
  completeness. (A pinned Semgrep ruleset lives at `.semgrep/itest.yml`; it is
  currently clean — look for what static rules miss.)
- **Robustness** — malformed/partial state, empty state, missing files,
  concurrent runs, huge state files, non-UTF8, unusual ARNs.
- **API/design** — is the engine/caller boundary clean? Are the manifest schema
  and the detector interface right for the MCP server and declaration sources
  still to come? Any leaky abstraction that will hurt later.
- **Test quality** — do the tests assert behavior or restate the code? Are the
  intended-red reference-API branches truly load-bearing? Any coverage gap on an
  invariant above.
- **Simplification** — duplicated logic, a module doing two jobs, anything that
  could be smaller without losing a guarantee.

## Output format

For each finding: `file:line` — one-sentence claim — a concrete failure scenario
(inputs → wrong result) — severity (critical / high / medium / low / nit).
Rank most-severe first. Distinguish a real defect from a style preference. If an
invariant above is fully upheld, say so explicitly — a clean bill on a
load-bearing invariant is a useful result to reconcile.
