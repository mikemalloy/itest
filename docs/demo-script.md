# Demo script (asciinema)

The exact command sequence to record for the ITest demo, with one sentence of
narration per step. Everything runs against the bundled fixture — no AWS, no
deploy. Record from a fresh clone.

Suggested capture:

```sh
asciinema rec itest-demo.cast
```

---

### 0. Setup

```sh
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

> "ITest installs as a single CLI with no services to stand up."

### 1. Plan

```sh
itest plan --tf-json tests/fixtures/simple-web-app-plan.json
```

> "`plan` reads the Terraform JSON and shows the three security-group edges it
> found — and honestly reports the seven resources no detector analyzed yet."

### 2. Look at the diagram

```sh
cat .itest/diagram.mmd
```

> "It also emits a Mermaid diagram of the integration chain: internet to ALB to
> web to database."

### 3. Sync

```sh
itest sync --auto-approve --tf-json tests/fixtures/simple-web-app-plan.json
```

> "`sync` generates a pytest stub per point and writes the diffable manifest —
> the same plan-then-apply rhythm as Terraform."

### 4. Inspect the generated stubs and manifest

```sh
ls itest_tests/                 # one file per point type
cat itest_tests/test_sg_edges.py
cat .itest/manifest.yaml
```

> "Each stub names the point it covers; the manifest records an ownership hash
> so ITest will never overwrite an edit you make here."

### 5. Verify (all stubs)

```sh
itest verify
```

> "`verify` runs the suite and reports coverage at the point level — right now
> all three are stubs."

### 6. Implement one test, then verify again

```sh
# replace one pytest.skip(...) with a real assertion, then:
itest verify
```

> "Turn one stub into a real check and that integration point flips to
> passing — coverage you can see per connection, not just per test."

### 7. Show the ownership guarantee (optional)

```sh
# edit a stub, remove an SG rule from a copy of the fixture, then:
itest sync --auto-approve --tf-json <modified-plan.json>
```

> "If the Terraform drops a rule, the covering test is flagged orphaned, never
> deleted — and your hand-edited test file is left untouched."
