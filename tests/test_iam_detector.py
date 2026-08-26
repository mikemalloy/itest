"""IAM edge detector tests against the sanitized alex state fixtures.

Expected counts below were derived by hand from the fixtures. If the detector
disagrees, either the parser is wrong or the fixture reading was — both
deserve a loud failure, never a silent adjustment of the expectation.
"""

from __future__ import annotations

import json
from pathlib import Path

from itest.core.detectors.base import detect_all
from itest.core.detectors.iam_edges import IamEdgeDetector

ALEX = Path(__file__).resolve().parent / "fixtures" / "alex"

LOGS_ARN = "arn:aws:logs:us-west-1:111111111111:*"
BASIC_EXEC = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
SAGEMAKER_FULL = "arn:aws:iam::aws:policy/AmazonSageMakerFullAccess"


def _load(name: str) -> dict:
    return json.loads((ALEX / name).read_text(encoding="utf-8"))


def _edges(points, source):
    return [p for p in points if p.source == source]


def _one(points, source, target):
    matches = [p for p in points if p.source == source and p.target == target]
    assert len(matches) == 1, f"expected one edge {source}->{target}, got {matches}"
    return matches[0]


def test_alex_s5_lambda_aurora_role_edges() -> None:
    points = IamEdgeDetector().detect(_load("alex-s5.json"))
    role = "aws_iam_role.lambda_aurora_role"
    edges = _edges(points, role)
    assert all(p.type == "iam_edge" for p in edges)

    resource_edges = [p for p in edges if not p.attributes.get("managed")]
    managed_edges = [p for p in edges if p.attributes.get("managed")]
    assert len(resource_edges) == 3, [p.target for p in resource_edges]
    assert len(managed_edges) == 1, [p.target for p in managed_edges]

    rds = _one(points, role, "aws_rds_cluster.aurora")
    assert rds.attributes["effect"] == "Allow"
    assert rds.attributes["actions"] == sorted(
        [
            "rds-data:ExecuteStatement",
            "rds-data:BatchExecuteStatement",
            "rds-data:BeginTransaction",
            "rds-data:CommitTransaction",
            "rds-data:RollbackTransaction",
        ]
    )
    assert rds.attributes["external"] is False
    assert rds.attributes["wildcard_action"] is False
    assert rds.attributes["wildcard_resource"] is False

    secret = _one(points, role, "aws_secretsmanager_secret.db_credentials")
    assert secret.attributes["actions"] == ["secretsmanager:GetSecretValue"]
    assert secret.attributes["external"] is False

    logs = _one(points, role, LOGS_ARN)
    assert logs.attributes["external"] is True
    assert logs.attributes["wildcard_resource"] is True
    assert logs.attributes["wildcard_action"] is False

    managed = _one(points, role, BASIC_EXEC)
    assert managed.attributes["managed"] is True
    assert managed.attributes["actions"] == ["<unresolved>"]
    assert managed.attributes["broad_managed_policy"] is False
    assert managed.hcl_address == "aws_iam_role_policy_attachment.lambda_basic"


def test_alex_s5_inline_and_standalone_policy_dedupe() -> None:
    # The same policy appears both as an inline_policy block on the role and
    # as a standalone aws_iam_role_policy — it must yield one edge set, not two.
    points = IamEdgeDetector().detect(_load("alex-s5.json"))
    ids = [p.id for p in points]
    assert len(ids) == len(set(ids))
    assert len(points) == 4


def test_alex_s6_lambda_agents_role_edges() -> None:
    points = IamEdgeDetector().detect(_load("alex-s6.json"))
    role = "aws_iam_role.lambda_agents_role"

    sqs = _one(points, role, "aws_sqs_queue.analysis_jobs")
    assert sqs.attributes["external"] is False
    assert "sqs:ReceiveMessage" in sqs.attributes["actions"]

    # Agents invoking agents: a wildcard function ARN.
    invoke = _one(points, role, "arn:aws:lambda:us-west-1:111111111111:function:alex-*")
    assert invoke.attributes["wildcard_resource"] is True
    assert invoke.attributes["actions"] == ["lambda:InvokeFunction"]

    bedrock = [
        p for p in _edges(points, role) if p.target.startswith("arn:aws:bedrock")
    ]
    assert len(bedrock) == 2
    assert all(p.attributes["wildcard_resource"] for p in bedrock)
    assert all(p.attributes["external"] for p in bedrock)

    # Cross-stack references (stage 2 endpoint, stage 5 cluster and secret).
    endpoint = _one(
        points,
        role,
        "arn:aws:sagemaker:us-west-1:111111111111:endpoint/alex-embedding-endpoint",
    )
    assert endpoint.attributes["external"] is True
    assert endpoint.attributes["wildcard_resource"] is False

    secrets = [
        p for p in _edges(points, role) if p.target.startswith("arn:aws:secretsmanager")
    ]
    assert len(secrets) == 1
    assert secrets[0].attributes["external"] is True

    cluster = [p for p in _edges(points, role) if p.target.startswith("arn:aws:rds")]
    assert len(cluster) == 1
    assert cluster[0].attributes["external"] is True

    # A statement with a resource LIST emits one edge per resource.
    s3 = [p for p in _edges(points, role) if p.target.startswith("arn:aws:s3:::")]
    assert len(s3) == 2

    managed = _one(points, role, BASIC_EXEC)
    assert managed.attributes["managed"] is True


def test_alex_s2_broad_managed_policy_flag() -> None:
    points = IamEdgeDetector().detect(_load("alex-s2.json"))
    edge = _one(points, "aws_iam_role.sagemaker_role", SAGEMAKER_FULL)
    assert edge.attributes["managed"] is True
    assert edge.attributes["broad_managed_policy"] is True
    assert edge.hcl_address == "aws_iam_role_policy_attachment.sagemaker_full_access"
    assert len(points) == 1


def test_ids_stable_across_runs() -> None:
    a = {p.id for p in IamEdgeDetector().detect(_load("alex-s6.json"))}
    b = {p.id for p in IamEdgeDetector().detect(_load("alex-s6.json"))}
    assert a == b
    assert len(a) >= 12


def test_deny_statements_and_wildcard_actions() -> None:
    plan = {
        "values": {
            "root_module": {
                "resources": [
                    {
                        "address": "aws_iam_role.r",
                        "mode": "managed",
                        "type": "aws_iam_role",
                        "name": "r",
                        "values": {
                            "name": "r",
                            "arn": "arn:aws:iam::111111111111:role/r",
                            "inline_policy": [
                                {
                                    "name": "p",
                                    "policy": json.dumps(
                                        {
                                            "Statement": [
                                                {
                                                    "Effect": "Deny",
                                                    "Action": "s3:*",
                                                    "Resource": "arn:aws:s3:::b",
                                                }
                                            ]
                                        }
                                    ),
                                }
                            ],
                        },
                    },
                    {
                        "address": "aws_s3_bucket.b",
                        "mode": "managed",
                        "type": "aws_s3_bucket",
                        "name": "b",
                        "values": {"arn": "arn:aws:s3:::b"},
                    },
                ]
            }
        }
    }
    points = IamEdgeDetector().detect(plan)
    assert len(points) == 1
    edge = points[0]
    assert edge.target == "aws_s3_bucket.b"
    assert edge.attributes["effect"] == "Deny"
    assert edge.attributes["wildcard_action"] is True
    assert edge.attributes["actions"] == ["s3:*"]
    assert edge.hcl_address == "aws_iam_role.r.inline_policy[p]"


def test_iam_types_no_longer_unanalyzed() -> None:
    _, unanalyzed = detect_all(_load("alex-s5.json"))
    for rtype in (
        "aws_iam_role",
        "aws_iam_role_policy",
        "aws_iam_role_policy_attachment",
    ):
        assert rtype not in unanalyzed
