# Running ITest inside your Terraform workflow

ITest is already Terraform-native on the read side: every command starts from
`terraform show -json`, so every integration point it knows about was declared
in HCL and applied through your pipeline — there is no second inventory to keep
in step, and nothing is discovered by crawling the account. This page is the
other side of that. Not what ITest reads, but where it runs and where its
results land: the shape that exists today, the shapes that are designed, and
the one that is merely possible.

## The four shapes

### CI step after `terraform apply`

**STATUS: built.** Every command below runs on `main` today, and
[`.github/workflows/pipeline-example.yml`](../.github/workflows/pipeline-example.yml)
is a copyable workflow that executes them against a committed fixture.

The job runs immediately after apply, in the same working directory, and starts
by capturing state: `terraform show -json > state.json`. Then `itest plan
--tf-json state.json` diffs what the detectors find against the committed
`.itest/manifest.yaml` and prints a changeset in Terraform's own vocabulary —
new points, unchanged points, resurrected points, orphan candidates, and a
census of resource types no detector analyzed. An **unexpected** changeset is
one where those first and fourth buckets are non-empty on a pipeline run: new
points means the apply created wiring nobody reviewed into the manifest, and
orphan candidates means wiring the manifest still claims has gone from
Terraform. On a repository where `itest sync` was run and committed alongside
the HCL change, a post-apply plan is all-unchanged. Making that a gate is one
step of glue today: plan's exit code reports whether it could *read* the state
(0 on success, 1 when the JSON is missing or malformed), not what it found, so
the pipeline reads `itest plan --output json` and fails the job when
`new_points` or `orphan_candidates` is non-empty. A non-zero exit from plan
itself on an unexpected changeset is designed, not built.

`itest sync --auto-approve` comes next only if the team wants stubs generated
in CI, and the default is no. Sync writes test files, and a test file is source
a human reviews and implements; generating one on a runner produces a stub that
nothing will ever fill in and a manifest change nobody approved. Sync belongs in
the developer's checkout, in the same commit as the HCL that made the point
exist. The example workflow does run it, for one reason: its fixture arrives
with no manifest at all, so without a sync there would be nothing for verify to
run.

Then `itest verify --environment <env> --output json > verify.json`, whose exit
code *is* a gate: 0 green, 1 when an integration point fails, 2 on a collection
error or a configuration problem. Finally `itest report --html --from
verify.json --out readiness.html` renders the readiness page, and the job
uploads it as an artifact. Report always exits 0 when the page renders — the
verdict on the page is deliberately not an exit code, because a report command
that could fail the build is a report command people stop running.

Credentials on the runner are read-only cloud credentials and nothing else: the
generated `conftest.py` builds its boto3 session from the profile recorded in
`.itest/skill-answers.yaml`, so leaving that profile empty falls through to
boto3's default chain, which on a runner is the environment the job was given.
An active-tier test credential is separate and never travels as a value — the
probes take the **name** of an environment variable and resolve it through
`itest.probes.credential`, seeded locally by a gitignored `.itest/.env`. The
same gitignored/committed split governs the environment machinery. From
[DESIGN.md](../DESIGN.md): *"The gate that lets an active (mutating) test tier
exist safely is the AND of two artifacts: a committed **policy**
(`.itest/environments.yaml`) saying which tiers each named environment may run,
and a local **binding** (a `--environment` flag, else the `.itest/environment`
file) saying where this checkout is pointed. A tier runs only when both allow
it."* The policy half is committed and code-reviewed — that is where a reviewer
vetoes a mutating tier before it exists. The binding half is the gitignored one,
so CI has to supply it: pass `--environment` from a repository variable, or
write the runner-local `.itest/environment` file in a step. Never commit either
one as a secret; neither of them is a secret, and the binding is not a project
fact.

### HCP Terraform / Terraform Enterprise run task (post-apply)

**STATUS: designed, not built** —
[#31](https://github.com/mikemalloy/itest/issues/31).

What it would do: HCP Terraform calls a registered run task after apply with a
callback URL and a pointer to the run. The task fetches that run's state,
executes the same plan-then-verify pair the CI step runs, and posts back a
pass/fail with a short message and a URL to the readiness page. The appeal over
the CI step is that it binds to the *run*, not to the pipeline that started it:
a console-initiated apply and a VCS-driven apply are both covered, and the
verdict lands on the run itself where the person who approved the apply is
already looking.

The open questions are what keep it designed. A run task needs an endpoint
reachable from HCP, and ITest is local-first with no server — so where that
endpoint runs (customer-hosted, or a service ITest does not have) is
unresolved. So is the credential boundary: the task would need read-only cloud
credentials of its own to run verify against the account, which is a standing
grant living somewhere other than a CI job's short-lived one. And the whole
shape is only worth building for teams on HCP or Terraform Enterprise at all,
which is a design-partner question, not an engineering one.

### Declarations in HCL

**STATUS: designed, not built** —
[#32](https://github.com/mikemalloy/itest/issues/32).

Today ITest infers every point from resources Terraform already manages, and the
committed declaration is the manifest that inference produces. The designed
alternative is to let a developer state something ITest cannot infer *in the HCL
itself* — either `itest:*` tags on a resource, which ride through `terraform
show -json` for free and need no provider at all, or a small provider exposing
declaration resources, which would be typed and validated by `terraform plan`
but is a real artifact to publish and version. The thing such a declaration would
carry is an agent tool: an MCP server's endpoint, the traits its tools claim,
which of them mutate. That per-server declaration file — `.itest/tools/<server>.yaml`
— is itself designed rather than built
([#26](https://github.com/mikemalloy/itest/issues/26)), so the HCL shape is
downstream of a shape that does not exist yet; the file comes first, and the
question of whether it should have been HCL all along comes after.

### `terraform test` as the harness

**STATUS: possible, low priority.** `terraform test` could invoke ITest from a
`run` block's `command` so verification lives in the same file as the
infrastructure it tests. It is low priority because that harness runs against
ephemeral test infrastructure it applies and destroys, whereas the release-day
question ITest answers is about the environment that is actually deployed.

## Where results land

The readiness page is a job artifact — one self-contained HTML file per run,
downloadable from the run that produced it, with no server behind it. The
durable artifacts are in the repository: `.itest/manifest.yaml` is the
integration inventory, diffable and code-reviewed like any other source file,
and the generated tests under `itest_tests/` are reviewed and implemented the
same way. `.itest/environments.yaml` is committed too, which is the point of
it — the policy that decides which tiers may run where is reviewable before it
can loose anything. What stays local and gitignored: the `.itest/environment`
binding, `.itest/skill-answers.yaml`, and `.itest/.env`.

One thing is genuinely open. The manifest and any run record carry resource
addresses, and on an applied state some of those are ARNs with an account id in
them. Committing the manifest is what makes the inventory reviewable, and
`itest redact` / `verify --redact` pseudonymize account ids on the way *out* of
an environment — but neither settles whether the committed manifest itself
should hold ARNs, or whether run records belong in the repository at all rather
than in whatever system already holds a team's release evidence. That depends on
how a given team treats resource identifiers, and it is a design-partner
question we are not resolving unilaterally.

## Evidence

- [docs/compatibility.md](compatibility.md) — every real state ITest has been
  run against, what it found, and the resource types it does not analyze yet.
  Pinned by tests so the table cannot drift from the code.
- [docs/report.md](report.md) — what the readiness page shows, where each
  number comes from, and the rule that nothing on it is illustrative.
