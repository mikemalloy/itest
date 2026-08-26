"""Tests for ``itest redact``.

Every credential in here is synthetic. The pattern-matched ones are obvious
fakes (they only need to match a prefix rule), and the generic high-entropy
token is generated from a seeded PRNG at import time rather than committed as
a literal, so this file contains nothing that resembles a real secret at rest.
"""

from __future__ import annotations

import json
import random
import re
import string
from pathlib import Path

import pytest
from typer.testing import CliRunner

from itest.cli import app
from itest.core import redact

runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "simple-web-app-plan.json"

ACCOUNT_A = "123456789012"
ACCOUNT_B = "210987654321"

# Obvious fakes: these are caught by shape, so they need no real entropy.
FAKE_OPENAI_KEY = "sk-notarealkeyaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
FAKE_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"  # AWS's own documented example key.
FAKE_BEARER = "eyJhbGciOiJub25lIn0.notarealtoken.notarealsignature"
FAKE_CONN_PASSWORD = "notarealpassword"
FAKE_DB_PASSWORD = "notarealdbpassword"
FAKE_SECRET_STRING = "notarealsecretstring"

# Generated, not committed: high entropy is the whole point of this one.
_rng = random.Random(20260825)
FAKE_GENERIC_TOKEN = "".join(
    _rng.choice(string.ascii_letters + string.digits) for _ in range(44)
)

ALL_SECRETS = [
    FAKE_OPENAI_KEY,
    FAKE_AWS_KEY,
    FAKE_BEARER,
    FAKE_CONN_PASSWORD,
    FAKE_DB_PASSWORD,
    FAKE_SECRET_STRING,
    FAKE_GENERIC_TOKEN,
]


def sample_document() -> dict:
    """A synthetic plan document carrying one of every redaction category."""
    return {
        "format_version": "1.0",
        "terraform_version": "1.9.0",
        "planned_values": {
            "root_module": {
                "resources": [
                    {
                        "address": "aws_db_instance.main",
                        "type": "aws_db_instance",
                        "name": "main",
                        "values": {
                            "identifier": "app-db",
                            "username": "appuser",
                            "master_password": FAKE_DB_PASSWORD,
                            "arn": f"arn:aws:rds:us-east-1:{ACCOUNT_A}:db:app-db",
                        },
                        "sensitive_values": {"master_password": True},
                    },
                    {
                        "address": "aws_lambda_function.api",
                        "type": "aws_lambda_function",
                        "name": "api",
                        "values": {
                            "function_name": "api",
                            "role": f"arn:aws:iam::{ACCOUNT_A}:role/api-exec",
                            "environment": [
                                {
                                    "variables": {
                                        "AWS_REGION": "us-east-1",
                                        "DATABASE_NAME": "appdb",
                                        "QUEUE_ARN": (
                                            f"arn:aws:sqs:us-east-1:{ACCOUNT_B}:jobs"
                                        ),
                                        "OPENAI_API_KEY": FAKE_OPENAI_KEY,
                                        "STRIPE_SECRET": FAKE_GENERIC_TOKEN,
                                        "DEBUG": "true",
                                    }
                                }
                            ],
                        },
                        "sensitive_values": {},
                    },
                    {
                        "address": "aws_ssm_parameter.notes",
                        "type": "aws_ssm_parameter",
                        "name": "notes",
                        "values": {
                            "access_key": FAKE_AWS_KEY,
                            # Outside Lambda env, so only the entropy rule can
                            # catch this one.
                            "api_token": FAKE_GENERIC_TOKEN,
                            "auth_header": f"Bearer {FAKE_BEARER}",
                            "conn": (
                                f"postgres://appuser:{FAKE_CONN_PASSWORD}"
                                "@db.example.com:5432/appdb"
                            ),
                            "owner_account": ACCOUNT_B,
                        },
                        "sensitive_values": {},
                    },
                ]
            }
        },
        "resource_changes": [
            {
                "address": "aws_secretsmanager_secret_version.api",
                "type": "aws_secretsmanager_secret_version",
                "change": {
                    "before": None,
                    "after": {
                        "name": "api-key",
                        "secret_string": FAKE_SECRET_STRING,
                    },
                    "after_sensitive": {"secret_string": True},
                },
            }
        ],
    }


def _resources(document: dict) -> dict[str, dict]:
    root = document["planned_values"]["root_module"]["resources"]
    return {r["address"]: r for r in root}


def _blob(document: dict) -> str:
    return json.dumps(document)


# --------------------------------------------------------------------------
# Category 1: Terraform's own sensitive_values
# --------------------------------------------------------------------------


def test_sensitive_values_are_redacted() -> None:
    clean, _ = redact.redact_document(sample_document())
    db = _resources(clean)["aws_db_instance.main"]
    assert db["values"]["master_password"] == redact.PLACEHOLDER
    # Its non-sensitive siblings survive.
    assert db["values"]["username"] == "appuser"
    assert db["values"]["identifier"] == "app-db"


def test_sensitive_values_in_resource_changes_are_redacted() -> None:
    clean, _ = redact.redact_document(sample_document())
    after = clean["resource_changes"][0]["change"]["after"]
    assert after["secret_string"] == redact.PLACEHOLDER
    assert after["name"] == "api-key"


# --------------------------------------------------------------------------
# Category 2: Lambda environment variables (allowlist only)
# --------------------------------------------------------------------------


def test_lambda_env_is_allowlist_only() -> None:
    clean, _ = redact.redact_document(sample_document())
    env = _resources(clean)["aws_lambda_function.api"]["values"]["environment"][0]
    variables = env["variables"]

    # Keys are preserved so the shape of the config stays readable.
    assert set(variables) == {
        "AWS_REGION",
        "DATABASE_NAME",
        "QUEUE_ARN",
        "OPENAI_API_KEY",
        "STRIPE_SECRET",
        "DEBUG",
    }
    # Allowlisted keys keep their values.
    assert variables["AWS_REGION"] == "us-east-1"
    assert variables["DATABASE_NAME"] == "appdb"
    # Everything else goes, including innocuous-looking keys: allowlist only.
    assert variables["OPENAI_API_KEY"] == redact.PLACEHOLDER
    assert variables["STRIPE_SECRET"] == redact.PLACEHOLDER
    assert variables["DEBUG"] == redact.PLACEHOLDER


def test_allowlisted_arn_env_value_is_still_pseudonymized() -> None:
    clean, _ = redact.redact_document(sample_document())
    env = _resources(clean)["aws_lambda_function.api"]["values"]["environment"][0]
    queue_arn = env["variables"]["QUEUE_ARN"]

    assert queue_arn.startswith("arn:aws:sqs:us-east-1:")
    assert queue_arn.endswith(":jobs")
    assert ACCOUNT_B not in queue_arn


# --------------------------------------------------------------------------
# Category 3: pattern-based credential scrubbing
# --------------------------------------------------------------------------


def test_credential_patterns_are_scrubbed() -> None:
    clean, _ = redact.redact_document(sample_document())
    blob = _blob(clean)
    for secret in ALL_SECRETS:
        assert secret not in blob, f"{secret[:12]}... survived redaction"


def test_high_entropy_token_is_scrubbed_but_hashes_survive() -> None:
    """Mixed-alphabet tokens go; hex digests, which are not secrets, stay."""
    digest = "a3f1" * 16  # 64 hex chars, the shape of a checksum
    document = sample_document()
    values = document["planned_values"]["root_module"]["resources"][2]["values"]
    values["source_code_hash"] = digest

    clean, _ = redact.redact_document(document)
    scrubbed = _resources(clean)["aws_ssm_parameter.notes"]["values"]

    assert scrubbed["api_token"] == redact.PLACEHOLDER
    assert scrubbed["source_code_hash"] == digest


def test_connection_string_keeps_shape_but_loses_password() -> None:
    clean, _ = redact.redact_document(sample_document())
    conn = _resources(clean)["aws_ssm_parameter.notes"]["values"]["conn"]
    assert conn.startswith("postgres://appuser:")
    assert conn.endswith("@db.example.com:5432/appdb")
    assert FAKE_CONN_PASSWORD not in conn


# --------------------------------------------------------------------------
# Category 4: account-ID pseudonymization
# --------------------------------------------------------------------------


def test_account_ids_are_pseudonymized_consistently() -> None:
    clean, _ = redact.redact_document(sample_document())
    resources = _resources(clean)

    db_arn = resources["aws_db_instance.main"]["values"]["arn"]
    role_arn = resources["aws_lambda_function.api"]["values"]["role"]
    queue_arn = resources["aws_lambda_function.api"]["values"]["environment"][0][
        "variables"
    ]["QUEUE_ARN"]
    owner = resources["aws_ssm_parameter.notes"]["values"]["owner_account"]

    db_account = db_arn.split(":")[4]
    role_account = role_arn.split(":")[4]
    queue_account = queue_arn.split(":")[4]

    # The same real account maps to the same fake everywhere, so ARNs still
    # correlate with each other after redaction.
    assert db_account == role_account
    # Distinct accounts stay distinct.
    assert queue_account != db_account
    assert owner == queue_account
    # And none of the real ones survive.
    blob = _blob(clean)
    assert ACCOUNT_A not in blob
    assert ACCOUNT_B not in blob


# --------------------------------------------------------------------------
# Category 5: structure is preserved
# --------------------------------------------------------------------------


def test_structure_and_addresses_are_untouched() -> None:
    original = sample_document()
    clean, _ = redact.redact_document(original)

    assert clean["format_version"] == "1.0"
    assert clean["terraform_version"] == "1.9.0"
    assert set(_resources(clean)) == set(_resources(original))
    for address, resource in _resources(clean).items():
        assert resource["type"] == _resources(original)[address]["type"]
        assert resource["name"] == _resources(original)[address]["name"]


def test_input_document_is_not_mutated() -> None:
    original = sample_document()
    redact.redact_document(original)
    db = _resources(original)["aws_db_instance.main"]
    assert db["values"]["master_password"] == FAKE_DB_PASSWORD


def test_redacted_real_fixture_still_parses_with_itest() -> None:
    """The output must remain a plan document ITest's own detector accepts."""
    from itest.core.detectors.base import detect_all

    original = json.loads(REAL_FIXTURE.read_text(encoding="utf-8"))
    clean, _ = redact.redact_document(original)

    before_points, _ = detect_all(original)
    after_points, after_unanalyzed = detect_all(clean)

    assert len(after_points) == len(before_points) == 3
    # Point identity is derived from addresses and rule content, both of which
    # redaction leaves alone, so the ids are unchanged.
    assert {p.id for p in after_points} == {p.id for p in before_points}
    assert after_unanalyzed  # unrelated resources still reported, not dropped


# --------------------------------------------------------------------------
# Idempotence
# --------------------------------------------------------------------------


def test_redact_is_idempotent() -> None:
    once, _ = redact.redact_document(sample_document())
    twice, findings = redact.redact_document(once)
    assert twice == once
    assert findings == [], "a redacted document should have nothing left to find"


def test_redact_of_real_fixture_is_idempotent() -> None:
    original = json.loads(REAL_FIXTURE.read_text(encoding="utf-8"))
    once, _ = redact.redact_document(original)
    twice, _ = redact.redact_document(once)
    assert twice == once


# --------------------------------------------------------------------------
# Findings
# --------------------------------------------------------------------------


def test_findings_cover_every_category() -> None:
    _, findings = redact.redact_document(sample_document())
    categories = {f.category for f in findings}
    assert categories == {
        "sensitive_value",
        "lambda_env",
        "credential_pattern",
        "account_id",
    }


def test_findings_never_contain_the_secret() -> None:
    """A --check report gets pasted into CI logs; it must not leak."""
    _, findings = redact.redact_document(sample_document())
    blob = json.dumps([f.model_dump() for f in findings])
    for secret in ALL_SECRETS:
        assert secret not in blob
    assert ACCOUNT_A not in blob
    assert ACCOUNT_B not in blob


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


@pytest.fixture
def sample_file(tmp_path: Path) -> Path:
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(sample_document()), encoding="utf-8")
    return path


def test_cli_writes_sanitized_output(sample_file: Path, tmp_path: Path) -> None:
    out = tmp_path / "clean.json"
    result = runner.invoke(app, ["redact", str(sample_file), "-o", str(out)])
    assert result.exit_code == 0, result.output

    blob = out.read_text(encoding="utf-8")
    for secret in ALL_SECRETS:
        assert secret not in blob
    json.loads(blob)  # still valid JSON


def test_cli_reads_stdin_writes_stdout(sample_file: Path) -> None:
    result = runner.invoke(
        app, ["redact"], input=sample_file.read_text(encoding="utf-8")
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    for secret in ALL_SECRETS:
        assert secret not in json.dumps(payload)


def test_cli_check_mode_exits_nonzero_and_writes_nothing(
    sample_file: Path, tmp_path: Path
) -> None:
    out = tmp_path / "clean.json"
    result = runner.invoke(app, ["redact", str(sample_file), "-o", str(out), "--check"])
    assert result.exit_code == 1, result.output
    assert not out.exists(), "--check must not write"
    assert "sensitive_value" in result.output
    for secret in ALL_SECRETS:
        assert secret not in result.output


def test_cli_check_mode_on_clean_file_exits_zero(tmp_path: Path) -> None:
    clean_doc, _ = redact.redact_document(sample_document())
    path = tmp_path / "clean.json"
    path.write_text(json.dumps(clean_doc), encoding="utf-8")

    result = runner.invoke(app, ["redact", str(path), "--check"])
    assert result.exit_code == 0, result.output


def test_cli_check_json_output(sample_file: Path) -> None:
    result = runner.invoke(
        app, ["redact", str(sample_file), "--check", "--output", "json"]
    )
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["finding_count"] > 0
    assert {f["category"] for f in payload["findings"]}


def test_cli_missing_input_is_config_error(tmp_path: Path) -> None:
    result = runner.invoke(app, ["redact", str(tmp_path / "nope.json")])
    assert result.exit_code == 2
    assert "not found" in result.output


def test_cli_invalid_json_is_config_error(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    result = runner.invoke(app, ["redact", str(path)])
    assert result.exit_code == 2
    assert "valid JSON" in result.output


# --------------------------------------------------------------------------
# Account-fingerprinting identifiers
# --------------------------------------------------------------------------

# Invented, and deliberately not starting with the pseudonym marker.
FAKE_PRINCIPAL_AIDA = "AIDAQWERTYUIOPASDFGHJ"
FAKE_PRINCIPAL_AROA = "AROAZXCVBNMASDFGHJKLQ"
FAKE_DBI_RESOURCE_ID = "db-QWERTYUIOPASDFGH"

ALL_IDENTIFIERS = [
    FAKE_PRINCIPAL_AIDA,
    FAKE_PRINCIPAL_AROA,
    FAKE_DBI_RESOURCE_ID,
]


def identifier_document() -> dict:
    """One of each account-fingerprinting identifier, plus a repeat."""
    return {
        "format_version": "1.0",
        "values": {
            "root_module": {
                "resources": [
                    {
                        "address": "aws_iam_role.app",
                        "type": "aws_iam_role",
                        "name": "app",
                        "values": {
                            "name": "app-role",
                            "unique_id": FAKE_PRINCIPAL_AROA,
                            "arn": f"arn:aws:iam::{ACCOUNT_A}:role/app-role",
                        },
                        "sensitive_values": {},
                    },
                    {
                        "address": "aws_iam_user.deploy",
                        "type": "aws_iam_user",
                        "name": "deploy",
                        "values": {
                            "unique_id": FAKE_PRINCIPAL_AIDA,
                            # The same principal referenced a second time, so
                            # correlation after pseudonymization is testable.
                            "policy_note": f"principal {FAKE_PRINCIPAL_AIDA} only",
                        },
                        "sensitive_values": {},
                    },
                    {
                        "address": "aws_db_instance.main",
                        "type": "aws_db_instance",
                        "name": "main",
                        "values": {
                            "identifier": "app-db",
                            "dbi_resource_id": FAKE_DBI_RESOURCE_ID,
                        },
                        "sensitive_values": {},
                    },
                ]
            }
        },
    }


def _id_resources(document: dict) -> dict[str, dict]:
    return {r["address"]: r for r in document["values"]["root_module"]["resources"]}


def test_identifiers_are_scrubbed() -> None:
    clean, _ = redact.redact_document(identifier_document())
    blob = json.dumps(clean)
    for identifier in ALL_IDENTIFIERS:
        assert identifier not in blob, f"{identifier} survived redaction"


def test_identifiers_keep_their_shape() -> None:
    """Pseudonymized, not blanked: the document stays readable as IAM/RDS."""
    clean, _ = redact.redact_document(identifier_document())
    resources = _id_resources(clean)

    role_id = resources["aws_iam_role.app"]["values"]["unique_id"]
    user_id = resources["aws_iam_user.deploy"]["values"]["unique_id"]
    dbi = resources["aws_db_instance.main"]["values"]["dbi_resource_id"]

    assert role_id.startswith("AROA")
    assert user_id.startswith("AIDA")
    assert dbi.startswith("db-")
    assert role_id != user_id
    # Length is preserved so the value still looks like what it is.
    assert len(role_id) == len(FAKE_PRINCIPAL_AROA)
    assert len(dbi) == len(FAKE_DBI_RESOURCE_ID)


def test_identifier_pseudonyms_correlate() -> None:
    """The same principal in two places maps to the same fake."""
    clean, _ = redact.redact_document(identifier_document())
    values = _id_resources(clean)["aws_iam_user.deploy"]["values"]

    assert values["unique_id"] in values["policy_note"]


def test_identifier_redaction_is_idempotent() -> None:
    once, _ = redact.redact_document(identifier_document())
    twice, findings = redact.redact_document(once)
    assert twice == once
    assert findings == []


def test_access_keys_are_still_credentials_not_identifiers() -> None:
    """AKIA/ASIA are secret access keys: blanked, never pseudonymized."""
    document = identifier_document()
    values = _id_resources(document)["aws_iam_user.deploy"]["values"]
    values["access_key"] = FAKE_AWS_KEY

    clean, _ = redact.redact_document(document)
    assert _id_resources(clean)["aws_iam_user.deploy"]["values"]["access_key"] == (
        redact.PLACEHOLDER
    )


def test_check_reports_identifiers_before_redaction(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(json.dumps(identifier_document()), encoding="utf-8")

    result = runner.invoke(app, ["redact", str(path), "--check"])
    assert result.exit_code == 1, result.output
    assert "identifier" in result.output
    # The report must not leak the values it is reporting.
    for identifier in ALL_IDENTIFIERS:
        assert identifier not in result.output


def test_alex_fixtures_have_no_bare_identifiers() -> None:
    """The checked-in real-world fixtures must be clean of these."""
    alex = REPO_ROOT / "tests" / "fixtures" / "alex"
    principal = re.compile(
        r"\b(?:AIDA|AROA|AGPA|AIPA|ANPA|ANVA|ABIA|ACCA)[0-9A-Z]{16,}\b"
    )
    dbi = re.compile(r"\bdb-[A-Z0-9]{10,}\b")

    for path in sorted(alex.glob("*.json")):
        blob = path.read_text(encoding="utf-8")
        for match in principal.findall(blob) + dbi.findall(blob):
            body = match.split("-", 1)[-1] if match.startswith("db-") else match[4:]
            assert body.startswith("EXAMPLE"), f"{path.name} still carries {match}"


def test_lambda_env_credential_is_attributed_to_lambda_env() -> None:
    """The env allowlist is the more specific reason, so it wins the label.

    A Lambda env value that also matches a credential pattern was reported as
    credential_pattern, because the generic string pass blanks it before the
    env allowlist ever sees it. Both redact it; the label was just wrong.
    """
    _, findings = redact.redact_document(sample_document())
    by_path = {f.path: f.category for f in findings}

    key_path = next(p for p in by_path if p.endswith("variables.OPENAI_API_KEY"))
    assert by_path[key_path] == "lambda_env"

    # Only one finding per path: the credential attribution is replaced, not
    # duplicated alongside the new one.
    assert sum(1 for f in findings if f.path == key_path) == 1


def test_lambda_env_attribution_does_not_change_what_is_redacted() -> None:
    clean, _ = redact.redact_document(sample_document())
    variables = _resources(clean)["aws_lambda_function.api"]["values"]["environment"][
        0
    ]["variables"]
    assert variables["OPENAI_API_KEY"] == redact.PLACEHOLDER
    assert variables["STRIPE_SECRET"] == redact.PLACEHOLDER
    assert variables["DEBUG"] == redact.PLACEHOLDER
    # Allowlisted keys keep their values and their own findings.
    assert variables["AWS_REGION"] == "us-east-1"
    assert variables["QUEUE_ARN"].startswith("arn:aws:sqs:")


def test_allowlisted_env_keeps_its_own_attribution() -> None:
    """An allowlisted ARN is still pseudonymized, and still says so."""
    _, findings = redact.redact_document(sample_document())
    arn_findings = [f for f in findings if f.path.endswith("variables.QUEUE_ARN")]
    assert [f.category for f in arn_findings] == ["account_id"]
