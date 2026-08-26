# ITest

ITest is a local-first CLI that reads your Terraform, extracts the integration
points it actually creates — security-group edges that say "A can reach B on
port N", IAM grants that say "this role can call that resource", and event
wiring that says "this queue triggers that function" — and turns them into a
tracked, testable inventory. It mirrors Terraform's own `plan` / `apply`
rhythm: `itest plan` shows you what it found, `itest sync` generates pytest
stubs and records them in a diffable manifest, and `itest verify` runs the
suite and reports coverage at the level of integration points, not just test
functions. It is built for infrastructure and platform engineers who want "is
this connection actually tested?" to have a real answer that lives in the repo.

![ITest's view of a real production stage](docs/demo/alex-s6-after-prompt13.png)

On a real production stage of 22 resources, Terraform sees a list. ITest sees
14 integration points, including the cross-stack dependencies that no single
state file describes.

## 60-second quickstart (no AWS needed)

Everything below runs against a checked-in `terraform show -json` fixture, so
you need neither AWS credentials nor a real deploy.

```sh
git clone <this-repo> && cd itest
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

### 1. Plan — see what ITest detects

```sh
itest plan --tf-json tests/fixtures/simple-web-app-plan.json
```

```
ITest plan: 3 new, 0 unchanged, 0 orphaned test(s).

New integration points (3):
  + [tcp:443 ingress] 0.0.0.0/0 -> aws_security_group.alb
      id=7a05510a0b6c  hcl=aws_security_group.alb.ingress[0]
  + [tcp:80 ingress] aws_security_group.alb -> aws_security_group.web
      id=f78fd45c158f  hcl=aws_security_group_rule.web_from_alb
  + [tcp:5432 ingress] aws_security_group.web -> aws_security_group.db
      id=ddd86d182890  hcl=aws_security_group_rule.db_from_web

Orphan candidates (0):
  (none)

Not analyzed (7 resource(s)):
  aws_db_instance  1
  aws_instance     2
  aws_lb           1
  aws_subnet       2
  aws_vpc          1
```

### 2. Sync — generate stubs and write the manifest

```sh
itest sync --auto-approve --tf-json tests/fixtures/simple-web-app-plan.json
```

```
...
Applied: added 3 stub(s), flagged 0 orphan(s), 0 human-modified file(s) preserved.
```

This creates `itest_tests/test_sg_edges.py` (three skipped stubs) and
`.itest/manifest.yaml` (the inventory + test registry).

Stubs are routed by point type — `itest_tests/test_<type>s.py` — so a project
with IAM grants and event wiring gets `test_iam_edges.py` and
`test_event_edges.py` alongside. This demo stack is security groups only, so
there is just the one file. The manifest records each test's real path; nothing
should assume a filename.

### 3. Verify — run the suite and report point coverage

```sh
itest verify
```

```
3 integration points: 0 passing, 0 failing, 0 errored, 3 stubs, 0 orphaned tests.
Ran 3 tests in 0.21s

Points:
  [STUB] 0.0.0.0/0 -> aws_security_group.alb (tcp:443 ingress)
  [STUB] aws_security_group.alb -> aws_security_group.web (tcp:80 ingress)
  [STUB] aws_security_group.web -> aws_security_group.db (tcp:5432 ingress)
```

Open `itest_tests/test_sg_edges.py`, replace a `pytest.skip(...)` with a real
assertion, and run `itest verify` again — that point flips to `[PASS]`.

## The integration chain

`itest plan` also writes `.itest/diagram.mmd`. For the demo stack it is:

```mermaid
flowchart LR
    n0["0.0.0.0/0"]
    n1["aws_security_group.alb"]
    n2["aws_security_group.web"]
    n3["aws_security_group.db"]
    n0 -->|tcp:443| n1
    n1 -->|tcp:80| n2
    n2 -->|tcp:5432| n3
```

## How it works

**`itest plan`** reads `terraform show -json` (from `--tf-json`, or by running
terraform in the current directory), runs every detector, and diffs the result
against the existing manifest. It prints a Terraform-style changeset — new
points, unchanged points, orphan candidates, and a count of resource types no
detector analyzed — then writes the proposal to `.itest/plan.json` and the
diagram to `.itest/diagram.mmd`. **Plan never modifies a test file or the
manifest.**

**`itest sync`** consumes that plan (running one implicitly when needed),
updates `.itest/manifest.yaml`, and generates a pytest stub for each new point.
It pauses for confirmation unless you pass `--auto-approve`.

**`itest verify`** runs the pytest suite under `itest_tests/`, maps each result
back to its integration point through the manifest, and rolls the results up:
a point is **passing** if at least one of its tests passed and none failed,
**failing** if any failed, and a **stub** if it is only skipped. It supports
`--output json` and `--output junit` (writes `itest-results.xml`), and exits
non-zero when anything fails.

**The manifest** (`.itest/manifest.yaml`) is the single shared artifact: a
human-readable, diffable inventory of every detected point and the tests
registered against it. You commit it alongside your Terraform.

**Ownership hashes** are how sync stays safe. When it generates a stub it
records the SHA-256 of the test file. On the next sync, if a file's hash no
longer matches what was recorded, ITest treats it as human-modified: it will
*append* new stubs but will never rewrite or delete a function you have
touched. Your edits are never clobbered.

**The orphan policy** is deliberately conservative. When a point disappears
from the Terraform (a rule was removed), the tests that covered it are flagged
`orphaned` in the manifest — never silently deleted. You decide whether to
remove the test or the assumption behind it.

## Agent skill

`itest sync` gets you stubs; something still has to write the assertions.
`skills/itest-implementer/` is a bundled agent skill that does that part: it
reads the manifest, finds the points still marked `[STUB]`, interviews you
once, and fills in the bodies with real checks against your live account.

It asks four questions, batched into a single prompt and saved to
`.itest/skill-answers.yaml` so a second run is silent:

1. Which AWS profile and region should the checks use?
2. Where is the Terraform directory this manifest describes?
3. Read-only API checks only, or also active probes that send real traffic?
4. Which environment is this — dev, staging, or prod?

**Generated checks are read-only by default** (`describe*`/`get*`/`list*`).
Active probes require an explicit yes, and answering "prod" to (4) forces the
read-only tier regardless. The skill never creates, modifies, or deletes AWS
resources, and shows you every generated body before running anything.

To use it from Claude Code in a consumer project, put it where the agent looks
for skills:

```sh
# symlink, so the skill tracks this repo
ln -s /path/to/itest/skills/itest-implementer .claude/skills/itest-implementer

# or copy it, to pin a version
cp -r /path/to/itest/skills/itest-implementer .claude/skills/
```

Then ask for what you want — "implement the ITest stubs", "make verify green"
— and the skill picks it up.

Add `.itest/skill-answers.yaml` to your `.gitignore`: it records local paths
and account details.

**The skill covers all three point types**, one recipe per detector, under
[`references/recipes/`](skills/itest-implementer/references/recipes/):
`sg_edge` asserts on security-group rules, `iam_edge` on policy simulation, and
`event_edge` on event source mappings, DLQ redrive policies, and Lambda
permissions. A point type with no recipe is reported and skipped, never guessed
at.

## Design decisions

**Why `plan` / `sync` mirrors Terraform.** Infrastructure engineers already
have a mental model for "show me the diff, then let me apply it." Reusing that
rhythm — a read-only proposal you inspect, then an explicit apply — means the
tool needs almost no new concepts, and it makes the destructive-looking step
(generating files) feel as safe as `terraform apply`.

**Why `path::TestClass::test_name` addressing.** Pytest's node-id syntax is the
one canonical address for a test that everything in the Python ecosystem
already understands. ITest resolves shorthand against the manifest registry
rather than parsing paths blind, and refuses to guess when input is ambiguous.

**Why local-first.** There is no server and no database. The truth lives in the
repo, in a file you can read, diff, and code-review. That makes ITest adoptable
one team at a time and keeps the manifest honest — it changes only when someone
commits a change.

**Why primitives before services.** ITest's three detectors each emit a typed
primitive rather than a high-level abstraction: `sg_edge` (source, target,
protocol, ports, direction), `iam_edge` (role, resource, actions), and
`event_edge` (source, target, mechanism). Higher-level "service" mappings and
composite checks are far more useful once the primitive layer is solid, so the
primitive layer comes first.

## Roadmap

All of the following are **planned, not built**. DESIGN.md's Scope ledger is
the authoritative record of what ships and what does not.

- **Detector tiers.** Tier 1 remainder: endpoint availability and DNS. Then
  composite detectors, and service mappings (e.g. SageMaker, App Runner) built
  on top of the primitives. (IAM edges and event wiring have since shipped.)
- **Labels & filtering** for slicing points and tests.
- **`itest add`** to declare integration points a detector can't infer.
- **`disable` / `enable`** commands for muting points with a recorded reason.
- **Saved-plan review flow** beyond the current implicit plan.
- **More of the agent/skill layer.** `itest-implementer` (above) ships with a
  recipe for every current detector; a recipe per *new* detector, and any
  deeper agent integration, are still to come.

## Development

Install the dev extras, then run the test suite and the linter:

```sh
pip install -e ".[dev]"

pytest                  # the test suite
ruff check .            # lint
ruff format --check .   # formatting
```

`ruff format .` (without `--check`) rewrites files in place. All three
commands must pass before a change is considered done — see
[DESIGN.md](DESIGN.md).

## Contributing

The extension point is the detector interface in
[`itest/core/detectors/base.py`](itest/core/detectors/base.py): implement
`detect(plan_json) -> list[IntegrationPoint]`, declare the resource types you
handle, and register your detector in `DETECTORS`. See
[`itest/core/detectors/sg_edges.py`](itest/core/detectors/sg_edges.py) for the
reference implementation and [`DESIGN.md`](DESIGN.md) for the decisions that
bound the project — its Scope ledger lists what is shipped and what is not to
be built without explicit instruction.
