---
name: Bug report
about: Something ITest does wrong — a bad detection, wrong verify result, a crash
title: ""
labels: bug
---

## What happened

<!-- The wrong behavior, in one or two sentences. -->

## Expected

<!-- What should have happened instead. -->

## Reproduction

<!-- Minimal steps. NEVER paste raw `terraform show/plan/state` or any
credential-bearing output. Redact first, or give grep counts and exit codes. -->

- ITest version / commit:
- Command run:
- Relevant resource type(s):

## Which invariant, if any

<!-- If this touches a load-bearing invariant (stable ids, sync never rewrites
human work, verify precedence, read-only/active gating, redaction, presentation
is a colorizer), name it — those are top priority. -->
