# Shared `conftest.py` template

Every recipe's generated tests take their fixtures from one `conftest.py`.
Write this file to `itest_tests/conftest.py` on the first generation, whatever
point types you are implementing, and do not write a second copy per recipe.

Two things live here:

- **`resolve_address`**, which turns the `hcl_address` and HCL-style `source` /
  `target` values recorded in the manifest into live resource values (the real
  `sg-…`, the real function ARN). Terraform addresses are not AWS ids, and
  pasting an id into a test breaks on the next destroy/apply cycle.
- **Read-only service clients**, one per service the recipes assert against.

Everything is read from `.itest/skill-answers.yaml`, written by the interview
step. **Never hardcode an account, profile, region, or directory here.**

<!-- BEGIN conftest.py -->
```python
"""Shared fixtures for ITest-generated integration tests.

Resolves the HCL addresses recorded in the manifest to live resource values by
reading `terraform show -json`, so assertions run against what is actually
deployed rather than against ids pasted into a test.

Configuration comes from .itest/skill-answers.yaml, written by the
itest-implementer skill. Nothing here is bound to an account or region, and
every client is used read-only.
"""

from __future__ import annotations

import json
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

import boto3
import pytest
import yaml

ANSWERS_PATH = Path(".itest/skill-answers.yaml")


@lru_cache(maxsize=1)
def load_answers() -> dict[str, Any]:
    """Return the interview answers recorded by the itest-implementer skill."""
    if not ANSWERS_PATH.exists():
        raise RuntimeError(
            f"{ANSWERS_PATH} not found. Run the itest-implementer skill first "
            "so it can record the AWS profile, region, and Terraform directory."
        )
    return yaml.safe_load(ANSWERS_PATH.read_text(encoding="utf-8")) or {}


@lru_cache(maxsize=8)
def _state_json(tf_dir: str) -> dict[str, Any]:
    """Run `terraform show -json` in ``tf_dir``, once per session per dir."""
    proc = subprocess.run(
        ["terraform", "show", "-json"],
        cwd=str(Path(tf_dir).expanduser()),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"`terraform show -json` failed in {tf_dir}:\n{proc.stderr.strip()}"
        )
    return json.loads(proc.stdout)


def _iter_resources(module: dict[str, Any]):
    """Yield every resource in a module tree, descending into child modules."""
    yield from module.get("resources", []) or []
    for child in module.get("child_modules", []) or []:
        yield from _iter_resources(child)


def _resource_address(hcl_address: str) -> str:
    """Reduce a point's hcl_address to the resource that owns it.

    ``aws_security_group.alb.ingress[0]`` -> ``aws_security_group.alb``
    ``aws_security_group_rule.db_from_web`` -> unchanged
    """
    address = hcl_address.split("[")[0]
    for suffix in (".ingress", ".egress"):
        if address.endswith(suffix):
            return address[: -len(suffix)]
    return address


def resolve_address(tf_dir: str, hcl_address: str) -> dict[str, Any]:
    """Return the live ``values`` for an HCL address, e.g. the real sg- id.

    Raises LookupError when the address is absent from the current state.
    That is itself a finding: the manifest and the deployment disagree.
    """
    wanted = _resource_address(hcl_address)
    root = _state_json(tf_dir).get("values", {}).get("root_module", {}) or {}
    for resource in _iter_resources(root):
        if resource.get("address") == wanted:
            return resource.get("values", {}) or {}
    raise LookupError(
        f"{wanted} is not in `terraform show -json` output for {tf_dir}. "
        "The manifest and the deployed state disagree."
    )


@pytest.fixture(scope="session")
def answers() -> dict[str, Any]:
    """The recorded interview answers."""
    return load_answers()


@pytest.fixture(scope="session")
def tf_dir(answers: dict[str, Any]) -> str:
    """The Terraform directory this manifest describes."""
    value = answers.get("terraform_dir")
    if not value:
        raise RuntimeError("terraform_dir missing from .itest/skill-answers.yaml")
    return str(value)


@pytest.fixture(scope="session")
def aws_session(answers: dict[str, Any]) -> boto3.Session:
    """A boto3 session bound to the configured profile and region."""
    return boto3.Session(
        profile_name=answers.get("aws_profile") or None,
        region_name=answers.get("aws_region") or None,
    )


@pytest.fixture(scope="session")
def ec2(aws_session: boto3.Session):
    """Read-only EC2 client (security groups)."""
    return aws_session.client("ec2")


@pytest.fixture(scope="session")
def iam(aws_session: boto3.Session):
    """Read-only IAM client (roles, policies, simulation)."""
    return aws_session.client("iam")


@pytest.fixture(scope="session")
def lambda_(aws_session: boto3.Session):
    """Read-only Lambda client. Named with a trailing underscore: `lambda`
    is a Python keyword and cannot be a fixture argument."""
    return aws_session.client("lambda")


@pytest.fixture(scope="session")
def sqs(aws_session: boto3.Session):
    """Read-only SQS client (queue attributes, redrive policy)."""
    return aws_session.client("sqs")


@pytest.fixture(scope="session")
def resolve(tf_dir: str):
    """``resolve(hcl_address)`` -> live values, bound to the configured dir."""

    def _resolve(hcl_address: str) -> dict[str, Any]:
        return resolve_address(tf_dir, hcl_address)

    return _resolve
```
<!-- END conftest.py -->

## Adding a client

A new recipe that asserts against a service ITest does not yet cover adds one
fixture here, in the same shape, and never a second `conftest.py`. Keep every
client read-only: the guardrails in SKILL.md forbid a generated test from
mutating anything.
