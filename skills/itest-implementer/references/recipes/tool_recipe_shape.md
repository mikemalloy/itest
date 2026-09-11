recipe-version: 1

# The shape of a generated tool recipe

**This is a template, not a recipe.** It fixes the shape every *generated*
tool-trait recipe takes, so the A2 (tenant isolation), B2 (destructive gating)
and B4 (audit record) recipes — and B1 observed — are written once, the same
way. No trait uses it yet: the generated-check registry in `itest/checks` is
empty, and `run_generated_check` answers every id with `not_verifiable` ("no
generated check for <id> yet").

## 1. Engine or generated?

Most tool traits are **engine checks**: ITest runs them from the manifest and the
live tool list, no file exists per tool, and nothing can go stale (A1, B1, D1–D3
today — see [`tool_authn.md`](tool_authn.md),
[`tool_mutation_class.md`](tool_mutation_class.md),
[`tool_provenance.md`](tool_provenance.md)).

A trait is **generated** only when its check needs a fact **only a person can
supply** — a record that belongs to a second tenant, how to read the audit sink,
which read tool shows whether a call changed anything. Everything else about the
check — the calls, the sentinels, the statuses, the scrubbing — is the library's,
exactly as for an engine check. The generated part is one line of binding and
one human-owned fixture.

## 2. The three parts, and who owns each

| part | lives in | written by | owned by |
|---|---|---|---|
| **the check** | `itest/checks/<family>.py`, registered in `GENERATED_CHECKS` | ITest | ITest — changed by upgrading ITest, never by editing a project |
| **the binding** | the stub file sync routes the trait to (active-tier traits get their own per-server file) | `itest sync` | ITest while unmodified (ownership hash); human-owned once edited |
| **the fixture** | `itest_tests/conftest.py` | a person, via the skill's interview and review gate | the person, always — sync never writes `conftest.py` |

**The ownership rule:** facts go in the fixture, logic goes in the library, and
the binding holds neither. If you find yourself wanting to edit a binding, the
change belongs in the fixture (a fact) or in ITest (logic). A binding someone
edited is human-owned from then on — sync will not regenerate it — which is
allowed, and is also how a binding stops receiving fixes.

## 3. The binding

One test function per (tool, trait), named by sync. The whole body is one call
and one assertion:

```python
from itest.checks import run_generated_check


def test_mcp_reference_mcp_delete_record_b2(itest_point, itest_target, b2_facts):
    """Integration point 3c0f6d2a91be (B2 destructive gating).

    reference-mcp/delete_record — generated binding; the check is ITest's,
    the facts are in conftest.py (b2_facts).
    """
    result = run_generated_check(
        "B2",
        itest_point("3c0f6d2a91be"),
        itest_target("reference-mcp"),
        fixtures=b2_facts,
    )
    assert result.status == "pass", f"[{result.status}] {result.detail}"
```

- The function **name** and the **docstring** are sync's and frozen — `itest
  verify` maps results to points by `path::test_name`, exactly as for every other
  stub (SKILL.md step 4).
- The binding asserts `pass`. Every other status — `fail`, `critical`,
  `changed`, `not_verifiable` — fails the test with the status and the detail,
  so a check that could not run never reads as coverage. How verify renders each
  status is verify's business, not the binding's.
- `result.detail` is already scrubbed; it is safe to print.

## 4. The fixtures

Two fixtures are shared by every generated binding and belong in the shared
`conftest.py` (listed in [`../conftest.md`](../conftest.md)):

```python
from pathlib import Path

from itest.core.declarations import load_declarations
from itest.core.declarations.tools import build_target
from itest.core.manifest import load_manifest


@pytest.fixture(scope="session")
def itest_point():
    """Return f(point_id) -> the manifest's mcp_tool point as the plain dict the
    check library takes (id, type, server, target, attributes)."""
    manifest = load_manifest(MANIFEST_PATH)
    points = {p.id: p for p in manifest.points if p.type == "mcp_tool"}

    def _point(point_id: str) -> dict:
        found = points.get(point_id)
        if found is None:
            raise LookupError(
                f"mcp_tool point {point_id} is not in {MANIFEST_PATH}; run `itest sync`."
            )
        return {
            "id": found.id,
            "type": found.type,
            "server": found.source,
            "target": found.target,
            "attributes": dict(found.attributes),
        }

    return _point


@pytest.fixture(scope="session")
def itest_target():
    """Return f(server) -> the McpTarget its declaration describes. The url and
    the credential are resolved by env-var NAME; neither is ever in this file."""
    declarations = {d.server: d for d in load_declarations(Path.cwd())}

    def _target(server: str):
        target = build_target(declarations[server], Path.cwd())
        if target is None:
            url_env = declarations[server].transport.url_env
            raise LookupError(f"{server} is unreachable: {url_env} is not set.")
        return target

    return _target
```

The third fixture is the **trait's own facts**, one per trait, named
`<trait>_facts` (`a2_facts`, `b2_facts`, `b4_facts`). It returns a plain dict
whose keys the trait's recipe lists. It is the only thing in the whole chain a
person writes:

```python
@pytest.fixture(scope="session")
def b2_facts():
    """B2 destructive gating, reference-mcp. Written by <name>, reviewed <date>.
    Keys are the ones tool_gating.md lists; nothing here is a secret."""
    return {"gate_parameter": "confirm"}
```

Facts are **never secrets**. A credential is the *name* of an environment
variable (the declaration already carries `auth.credential_env` and
`auth.second_tenant_env`); a record id is a sentinel or a record that exists to
be read. A missing key is the check's problem to report — it returns
`not_verifiable` naming the key — never the fixture's to paper over with a
default.

## 5. The fixtures interview

Each generated recipe lists its facts as questions, and the skill asks them in
the one batched interview (SKILL.md step 3), only for traits that apply to at
least one tool, and only questions `.itest/skill-answers.yaml` does not already
answer. The recipe states, for each fact: the question, why a person has to
answer it, a safe default if one exists (most have none), and what the check does
without it. Never accept a token; record an env-var name.

## 6. Lifecycle

| state | how you get there | what the check returns |
|---|---|---|
| **generated** | sync wrote the binding; no `<trait>_facts` fixture yet | the test errors at setup naming the missing fixture — nothing ran, and it says so |
| **awaiting check** | the fixture exists but this ITest has no generated check for the trait | `not_verifiable` — "no generated check for <id> yet" |
| **bound** | fixture present, check shipped, binding unmodified | the check's real status |
| **human-modified** | someone edited the binding | whatever the edit does; sync no longer touches it (ownership hash) |
| **orphaned** | the tool left the listing, or the trait stopped applying | flagged by sync, never deleted — the same rule as every other test |

A binding moves from *awaiting check* to *bound* by upgrading ITest, with no
project change at all. That is the point of keeping logic out of it.

## 7. The sections a generated recipe carries

Follow `http_probe.md` and the engine recipes:

1. `recipe-version:` line, then the **tier banner** (almost always `active`: the
   facts exist because the check has to act).
2. **What the check proves**, and **what it does not**.
3. **The facts** — each key of `<trait>_facts`, as an interview question (§5).
4. **Statuses**, and what each means for a reviewer.
5. **Evidence fields.**
6. **Standards mapping** — OWASP Agentic Top 10 id, LLM Top 10 where relevant,
   Semgrep cheatsheet rows, AgBOM attribute.
7. **How to generate the test** — the binding (§3) and the fixture (§4); the
   binding is sync's, so the skill writes only the fixture, after the review gate.
8. **What the reviewer does with a failure.**
9. **Reference server** — which tool in `examples/reference-mcp/` exercises each
   branch.
