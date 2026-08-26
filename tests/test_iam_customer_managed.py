"""Customer-managed policy attachments resolve to real edges.

An `aws_iam_role_policy_attachment` pointing at an AWS-managed ARN is opaque:
the document lives in AWS and there is nothing to read. One pointing at an
`aws_iam_policy` in the same state is not — the policy JSON is right there.
Treating both as "managed policy, actions unresolved" threw away everything
ITest could say about three of AWS's four serverless-patterns Terraform
samples, which grant through exactly this shape.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from itest.core.detectors.base import detect_all

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "customer-managed-policy-state.json"

ROLE = "aws_iam_role.lambda_exec"
POLICY = "aws_iam_policy.lambda_policy"
POLICY_ARN = "arn:aws:iam::111111111111:policy/demo-lambda-policy"
QUEUE = "aws_sqs_queue.jobs"
LOGS_ARN = "arn:aws:logs:us-east-1:111111111111:*"
BASIC_EXECUTION = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"


def _document() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _iam_edges(document: dict) -> list:
    points, _ = detect_all(document)
    return [p for p in points if p.type == "iam_edge"]


def _by_target(edges: list) -> dict:
    return {p.target: p for p in edges}


def _resources(document: dict) -> list:
    return document["values"]["root_module"]["resources"]


# --------------------------------------------------------------------------
# 1. The customer-managed policy is resolved
# --------------------------------------------------------------------------


def test_role_yields_three_edges() -> None:
    edges = _iam_edges(_document())
    assert len(edges) == 3, [p.target for p in edges]
    assert all(p.source == ROLE for p in edges)


def test_queue_grant_is_resolved_to_the_queue() -> None:
    edge = _by_target(_iam_edges(_document()))[QUEUE]
    assert edge.attributes["actions"] == ["sqs:SendMessage"]
    assert edge.attributes["external"] is False
    assert edge.attributes["wildcard_resource"] is False
    assert edge.attributes["managed"] is False
    assert edge.attributes["effect"] == "Allow"
    # The reader can see where the grant actually lives.
    assert edge.attributes["via_policy"] == POLICY
    # And the edge is attributed to the attachment that binds it.
    assert edge.hcl_address == "aws_iam_role_policy_attachment.lambda_policy"


def test_logs_grant_keeps_its_wildcard_arn() -> None:
    edge = _by_target(_iam_edges(_document()))[LOGS_ARN]
    assert edge.attributes["wildcard_resource"] is True
    assert edge.attributes["external"] is True
    assert edge.attributes["wildcard_action"] is True
    assert edge.attributes["via_policy"] == POLICY


def test_aws_managed_attachment_stays_opaque() -> None:
    edge = _by_target(_iam_edges(_document()))[BASIC_EXECUTION]
    assert edge.attributes["managed"] is True
    assert edge.attributes["actions"] == ["<unresolved>"]
    assert edge.attributes["broad_managed_policy"] is False
    assert edge.attributes["external"] is False
    assert "via_policy" not in edge.attributes


def test_no_edge_targets_the_policy_itself() -> None:
    """A policy is a container for grants, not a thing a role reaches."""
    targets = [p.target for p in _iam_edges(_document())]
    assert POLICY_ARN not in targets
    assert POLICY not in targets


# --------------------------------------------------------------------------
# 2. A customer policy that is not in this document
# --------------------------------------------------------------------------


def test_absent_customer_policy_is_managed_and_external() -> None:
    """Another stack owns it: nothing to read, and say so."""
    document = _document()
    _resources(document)[:] = [
        r for r in _resources(document) if r["type"] != "aws_iam_policy"
    ]

    edges = _by_target(_iam_edges(document))
    edge = edges[POLICY_ARN]
    assert edge.attributes["managed"] is True
    assert edge.attributes["external"] is True
    assert edge.attributes["actions"] == ["<unresolved>"]


def test_absent_aws_managed_policy_is_not_external() -> None:
    """arn:aws:iam::aws:policy/... is never in anyone's state, by design."""
    edge = _by_target(_iam_edges(_document()))[BASIC_EXECUTION]
    assert edge.attributes["external"] is False


# --------------------------------------------------------------------------
# 3. An unattached policy grants nothing
# --------------------------------------------------------------------------


def test_unattached_policy_emits_no_edges() -> None:
    document = _document()
    _resources(document)[:] = [
        r for r in _resources(document) if r["type"] != "aws_iam_role_policy_attachment"
    ]
    assert _iam_edges(document) == []


# --------------------------------------------------------------------------
# 4. Other binding shapes resolve the same way
# --------------------------------------------------------------------------


def test_managed_policy_arns_on_the_role_resolves() -> None:
    """The role's own list binds a policy just as an attachment does."""
    document = _document()
    resources = _resources(document)
    resources[:] = [
        r
        for r in resources
        if r["address"] != "aws_iam_role_policy_attachment.lambda_policy"
    ]
    for resource in resources:
        if resource["type"] == "aws_iam_role":
            resource["values"]["managed_policy_arns"] = [POLICY_ARN]

    edges = _by_target(_iam_edges(document))
    assert QUEUE in edges
    assert edges[QUEUE].attributes["via_policy"] == POLICY
    # Attributed to the role, which is where this binding is declared.
    assert edges[QUEUE].hcl_address == ROLE


def test_plural_policy_attachment_resolves() -> None:
    """aws_iam_policy_attachment carries a roles list rather than one role."""
    document = _document()
    resources = _resources(document)
    resources[:] = [
        r
        for r in resources
        if r["address"] != "aws_iam_role_policy_attachment.lambda_policy"
    ]
    resources.append(
        {
            "address": "aws_iam_policy_attachment.lambda_policy",
            "mode": "managed",
            "type": "aws_iam_policy_attachment",
            "name": "lambda_policy",
            "values": {
                "name": "demo-attach",
                "roles": ["demo-lambda-exec"],
                "policy_arn": POLICY_ARN,
            },
            "sensitive_values": {},
        }
    )

    edges = _by_target(_iam_edges(document))
    assert QUEUE in edges
    assert edges[QUEUE].attributes["via_policy"] == POLICY
    assert edges[QUEUE].hcl_address == "aws_iam_policy_attachment.lambda_policy"


# --------------------------------------------------------------------------
# 5. Housekeeping: ids, dedupe, unanalyzed
# --------------------------------------------------------------------------


def test_iam_policy_is_a_handled_type() -> None:
    _, unanalyzed = detect_all(_document())
    assert "aws_iam_policy" not in unanalyzed
    assert "aws_iam_role_policy_attachment" not in unanalyzed


def test_ids_are_stable_across_runs() -> None:
    first = sorted(p.id for p in _iam_edges(_document()))
    second = sorted(p.id for p in _iam_edges(_document()))
    assert first == second
    assert len(set(first)) == len(first)


def test_via_policy_edge_does_not_collide_with_an_inline_grant() -> None:
    """The same grant reached two ways must not share one point id.

    Otherwise moving a statement from an inline policy into a customer-managed
    one would look like no change at all.
    """
    document = _document()
    inline = copy.deepcopy(document)
    # Strip the attachment and express the same grant inline on the role.
    resources = _resources(inline)
    policy_json = next(
        r["values"]["policy"] for r in resources if r["type"] == "aws_iam_policy"
    )
    resources[:] = [
        r
        for r in resources
        if r["type"] not in {"aws_iam_policy", "aws_iam_role_policy_attachment"}
    ]
    for resource in resources:
        if resource["type"] == "aws_iam_role":
            resource["values"]["inline_policy"] = [
                {"name": "demo-lambda-policy", "policy": policy_json}
            ]

    via = {p.target: p.id for p in _iam_edges(document)}
    direct = {p.target: p.id for p in _iam_edges(inline)}
    assert via[QUEUE] != direct[QUEUE], "via_policy must take part in the id"


def test_duplicate_binding_yields_one_edge() -> None:
    """Declared as both an attachment and a managed_policy_arns entry."""
    document = _document()
    for resource in _resources(document):
        if resource["type"] == "aws_iam_role":
            resource["values"]["managed_policy_arns"] = [POLICY_ARN, BASIC_EXECUTION]

    edges = _iam_edges(document)
    assert len(edges) == 3, [p.target for p in edges]
    assert len({p.id for p in edges}) == 3


@pytest.mark.parametrize(
    "fixture,expected",
    [("alex-s2", 1), ("alex-s5", 4), ("alex-s6", 12)],
)
def test_alex_iam_edge_counts_unchanged(fixture: str, expected: int) -> None:
    """The alex fixtures carry no aws_iam_policy, so nothing about them moves."""
    document = json.loads(
        (REPO_ROOT / "tests" / "fixtures" / "alex" / f"{fixture}.json").read_text(
            encoding="utf-8"
        )
    )
    assert len(_iam_edges(document)) == expected
