# Security Policy

ITest is a tool security engineers point at their own infrastructure, so the
guarantees it makes about credentials and mutation are part of its security
surface, not just its documentation. Both are covered below.

## Supported versions

| Version | Supported |
| ------- | --------- |
| 0.2.x   | Yes       |
| < 0.2   | No        |

## Reporting a vulnerability

Report privately through GitHub's private vulnerability reporting:

**<https://github.com/mikemalloy/itest/security/advisories/new>**

(equivalently, the "Report a vulnerability" button on this repository's
Security tab).

Please do not open a public issue for a suspected vulnerability. A public
issue is the wrong channel for anything that needs a fix before it is widely
known — use the advisory form above, and it will be read.

Useful things to include: the version, the command or recipe involved, what an
attacker gets, and the smallest reproduction you have.

### What to expect

- **Acknowledgement within 3 business days** of the report.
- For a confirmed issue, **a fix or a written mitigation plan within 14 days**.
- **Credit on request** — say how you would like to be named in the advisory,
  or say you would rather not be.

## Scope

In scope:

- the `itest` CLI and the `itest` Python package
- the bundled `itest-implementer` skill and its recipes
- the GitHub Actions workflows in this repository

Out of scope:

- your own AWS infrastructure, and any finding ITest reports about it — that
  is the tool working, and it belongs to you, not here
- the tests ITest generates into your repository: they live in your repo, run
  in your CI, and use your credentials

## What ITest does with your credentials

ITest is **read-only by default** — the permissions its generated tests need
are `describe*`, `get*`, `list*`, and `iam:SimulatePrincipalPolicy`, and
"[a]ctive probes need an explicit yes" ([README](README.md)). Mutating
(`active`) tests run only where a committed policy allows that tier *and* a
local binding selects that environment; "[a]n unknown tier, an unsupported
version, a binding naming an undefined environment, and a `production: true`
environment that lists `active` are all hard errors raised when the policy is
loaded — verify refuses to start" ([DESIGN.md](DESIGN.md), Environment
profiles), and an environment named `prod` or `production` is treated as
production whether or not the flag was remembered. A test credential is never
handed to the tool: the skill records only the *name* of an environment
variable, the value is resolved at run time from the shell environment or a
gitignored `.itest/.env`, and "it never stores, logs, or echoes it"
(`itest/probes/credential.py`). For output, `verify --redact` "pseudonymizes
account IDs and high-entropy tokens (including in a failing test's assertion
detail) in every format" ([README](README.md)) — it deliberately does not strip
human-readable resource names such as bucket, cluster, and secret names.
