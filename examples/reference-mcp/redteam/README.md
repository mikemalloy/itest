# Red-team harness for the reference MCP server

This directory is a small, pinned prompt-injection harness: promptfoo drives
an agent that holds `reference-mcp`'s eight tools and tries, three ways, to
talk it into calling `delete_record`. Its results file is what ITest reads as
an **evidence source** (`../.itest/sources/promptfoo-ci.yaml`) and shows as
the *external evidence* lane beside the boundary checks on the readiness page.

**The rule, in one sentence: this evidence never counts** — it has no
lifecycle state, never changes VERIFIED, the verdict or coverage, and a clean
run can never turn a cell green (`docs/evidence.md`).

| file | what it is |
|---|---|
| `promptfooconfig.yaml` | four providers (two models × clean / poisoned tool results) over three cases |
| `prompt.json` | the chat-array prompt: the agent's system prompt plus the user turn |
| `agent.js` | a minimal MCP-connected agent as a promptfoo custom provider; the target |
| `poison-proxy.py` | a transparent stdio proxy (stdlib only) that can poison `get_guide`'s result |
| `proxy-clean.sh` / `proxy-poisoned.sh` | launch the server behind the proxy with `POISON=0` / `POISON=1` |
| `corpus.md` | the three cases, why each exists, and the 2026-09-23 result |

Nothing here holds an absolute path: the scripts resolve the proxy and the
server from their own directory and take `python3` from `PATH`, so the same
files run on a Mac and on `ubuntu-latest` unchanged. A test holds them to it.

## Running it locally

From the repository root, with the example's dependencies installed
(`pip install -e '.[examples]'`) and the key in your environment — it is read
from `ANTHROPIC_API_KEY` by name and never written anywhere:

```sh
cd examples/reference-mcp/redteam
AGENT_MODEL=claude-sonnet-5 AGENT_MODEL_2=claude-haiku-4-5-20251001 \
  npx -y promptfoo@0.123.1 eval -c promptfooconfig.yaml -o /tmp/r.json --no-cache
```

Then join the results onto the example's manifest and render the page:

```sh
cd ..
export ITEST_PROMPTFOO_RESULTS=/tmp/r.json
itest sync --auto-approve --allow-unreachable   # the stdio server alone
itest verify
itest report --html
```

`sync` prints one line for the source (`evidence promptfoo-ci (promptfoo):
run … 12 rows, N tools matched …`), and the page shows the lane under each
tool the agent called. With the variable unset the line reads `unreadable:
results_env ITEST_PROMPTFOO_RESULTS is not set` and everything else is
unchanged — that is every PR and every checkout that has not run the harness.

## What it costs

Twelve cells (four providers × three cases), each a handful of short model
calls: a few cents per run and about twenty seconds of model time. The
2026-09-23 run took 18 s end to end.

## In CI

`.github/workflows/redteam-nightly.yml` runs exactly the command above on a
daily schedule and on demand (`workflow_dispatch`), then `itest sync`, `itest
verify` (readonly tier) and `itest report --html`, and uploads the results
file and the page as artifacts. It gates nothing and never runs on a pull
request. `--no-cache` keeps promptfoo from writing a cache that an artifact
could pick up; the only secret is the key, and it reaches the job as an
environment variable and nothing else.
