"""Redaction must not destroy referential integrity.

A generated resource name — an S3 bucket, a queue with a random suffix — looks
exactly like a secret to an entropy heuristic. Replacing every one with the
same constant broke two things at once: a notification could no longer be
resolved to the bucket it names, and two distinct secrets collapsed into the
same string. Opaque tokens are now pseudonymized consistently, and a value that
appears as the tail of an ARN in the same document is treated as an identifier
rather than a secret.
"""

from __future__ import annotations

import json

from itest.core import redact

BUCKET_NAME = "s3-sqs-lambda-tf-sources3bucket-20260827185442152600000001"
BUCKET_ARN = f"arn:aws:s3:::{BUCKET_NAME}"

# Two distinct opaque blobs, neither backed by an ARN anywhere.
SECRET_ONE = "Zx8qL2mNvR7tYw4pKj9sHb3dFg6aQe1uOi5cXz0nMlPr"
SECRET_TWO = "Qw3rTy7uIo1pAs5dFg9hJk2lZx6cVb0nMq4wEr8tYu3i"


def bucket_document() -> dict:
    """A bucket whose name is also the tail of its own ARN, plus a notifier."""
    return {
        "format_version": "1.0",
        "values": {
            "root_module": {
                "resources": [
                    {
                        "address": "aws_s3_bucket.source",
                        "mode": "managed",
                        "type": "aws_s3_bucket",
                        "name": "source",
                        "values": {
                            "bucket": BUCKET_NAME,
                            "id": BUCKET_NAME,
                            "arn": BUCKET_ARN,
                        },
                        "sensitive_values": {},
                    },
                    {
                        "address": "aws_s3_bucket_notification.source",
                        "mode": "managed",
                        "type": "aws_s3_bucket_notification",
                        "name": "source",
                        "values": {"bucket": BUCKET_NAME, "id": BUCKET_NAME},
                        "sensitive_values": {},
                    },
                ]
            }
        },
    }


def secrets_document() -> dict:
    return {
        "format_version": "1.0",
        "values": {
            "root_module": {
                "resources": [
                    {
                        "address": "aws_ssm_parameter.two",
                        "mode": "managed",
                        "type": "aws_ssm_parameter",
                        "name": "two",
                        "values": {
                            "first": SECRET_ONE,
                            "second": SECRET_TWO,
                            "first_again": SECRET_ONE,
                        },
                        "sensitive_values": {},
                    }
                ]
            }
        },
    }


def _resources(document: dict) -> dict[str, dict]:
    return {r["address"]: r for r in document["values"]["root_module"]["resources"]}


# --------------------------------------------------------------------------
# (a) An ARN-backed identifier survives intact
# --------------------------------------------------------------------------


def test_arn_backed_bucket_name_is_not_scrubbed() -> None:
    """The ARN is kept, so the name that forms its tail must be kept too."""
    clean, _ = redact.redact_document(bucket_document())
    bucket = _resources(clean)["aws_s3_bucket.source"]["values"]

    assert bucket["arn"] == BUCKET_ARN
    assert bucket["bucket"] == BUCKET_NAME
    assert bucket["id"] == BUCKET_NAME


def test_notification_still_resolves_to_its_bucket() -> None:
    """Referential integrity: the notifier names the bucket by that string."""
    clean, _ = redact.redact_document(bucket_document())
    resources = _resources(clean)
    bucket = resources["aws_s3_bucket.source"]["values"]
    notification = resources["aws_s3_bucket_notification.source"]["values"]

    assert notification["bucket"] == bucket["bucket"]
    assert notification["bucket"] == bucket["arn"].rsplit(":", 1)[-1]


# --------------------------------------------------------------------------
# (b) Distinct secrets get distinct pseudonyms
# --------------------------------------------------------------------------


def test_two_secrets_do_not_collide() -> None:
    clean, _ = redact.redact_document(secrets_document())
    values = _resources(clean)["aws_ssm_parameter.two"]["values"]

    assert SECRET_ONE not in json.dumps(clean)
    assert SECRET_TWO not in json.dumps(clean)
    assert values["first"] != values["second"], "distinct secrets collided"


def test_the_same_secret_maps_consistently() -> None:
    """Same input, same output — so repeated references still correlate."""
    clean, _ = redact.redact_document(secrets_document())
    values = _resources(clean)["aws_ssm_parameter.two"]["values"]
    assert values["first"] == values["first_again"]


def test_pseudonym_is_recognisably_a_redaction() -> None:
    clean, _ = redact.redact_document(secrets_document())
    values = _resources(clean)["aws_ssm_parameter.two"]["values"]
    assert values["first"].startswith("redacted-")
    # Non-reversible: a short digest, not an encoding of the input.
    assert SECRET_ONE[:8] not in values["first"]


def test_findings_still_report_the_category_unchanged() -> None:
    _, findings = redact.redact_document(secrets_document())
    categories = {f.category for f in findings}
    assert categories == {"credential_pattern"}


# --------------------------------------------------------------------------
# (c) Idempotence
# --------------------------------------------------------------------------


def test_redacting_redacted_output_changes_nothing() -> None:
    once, _ = redact.redact_document(secrets_document())
    twice, findings = redact.redact_document(once)
    assert twice == once
    assert findings == []


def test_bucket_document_is_idempotent() -> None:
    once, _ = redact.redact_document(bucket_document())
    twice, findings = redact.redact_document(once)
    assert twice == once
    assert findings == []


# --------------------------------------------------------------------------
# A secret that merely looks like an ARN tail is still a secret
# --------------------------------------------------------------------------


def test_token_not_backed_by_an_arn_is_still_scrubbed() -> None:
    """Only an actual ARN tail earns the exemption."""
    document = bucket_document()
    _resources(document)["aws_s3_bucket.source"]["values"]["token"] = SECRET_ONE

    clean, _ = redact.redact_document(document)
    values = _resources(clean)["aws_s3_bucket.source"]["values"]
    assert values["token"].startswith("redacted-")
    assert values["bucket"] == BUCKET_NAME
