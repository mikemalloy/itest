"""Environment profiles: a committed tier policy and a local binding.

The policy (`.itest/environments.yaml`) says which tiers each named environment
may run; the binding (a `--environment` flag or the local `.itest/environment`
file) says where THIS checkout is pointed. The gate that lets an active tier
exist safely is the AND of the two: an active test runs only when a committed
policy allows it *and* the checkout is bound to an environment that allows it.
Absence of either always resolves to the safe floor — static and readonly only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from itest.core import environments as env

EXAMPLE_POLICY = """\
version: 1
environments:
  dev:    { tiers: [static, readonly, active] }
  stage:  { tiers: [static, readonly, active] }
  prod:   { tiers: [static, readonly], production: true }
"""


def _write_policy(base: Path, text: str = EXAMPLE_POLICY) -> Path:
    (base / ".itest").mkdir(parents=True, exist_ok=True)
    path = env.policy_path(base)
    path.write_text(text, encoding="utf-8")
    return path


def _write_binding(base: Path, name: str) -> None:
    (base / ".itest").mkdir(parents=True, exist_ok=True)
    env.binding_path(base).write_text(name + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# Loader round-trip
# --------------------------------------------------------------------------


def test_policy_loads_the_example(tmp_path: Path) -> None:
    policy = env.load_policy(_write_policy(tmp_path))
    assert set(policy.environments) == {"dev", "stage", "prod"}
    assert policy.environments["dev"].tiers == ["static", "readonly", "active"]
    assert policy.environments["prod"].tiers == ["static", "readonly"]
    assert policy.environments["prod"].production is True
    assert policy.environments["dev"].production is False


# --------------------------------------------------------------------------
# Validation, all at load time
# --------------------------------------------------------------------------


def test_unknown_tier_is_a_load_error(tmp_path: Path) -> None:
    text = "version: 1\nenvironments:\n  dev: { tiers: [static, turbo] }\n"
    with pytest.raises(env.EnvironmentConfigError, match="turbo"):
        env.load_policy(_write_policy(tmp_path, text))


def test_production_with_active_is_refused_at_load(tmp_path: Path) -> None:
    text = (
        "version: 1\nenvironments:\n"
        "  live: { tiers: [static, readonly, active], production: true }\n"
    )
    with pytest.raises(env.EnvironmentConfigError, match="active"):
        env.load_policy(_write_policy(tmp_path, text))


def test_name_prod_implies_production_even_without_the_flag(tmp_path: Path) -> None:
    text = "version: 1\nenvironments:\n  prod: { tiers: [static, readonly] }\n"
    policy = env.load_policy(_write_policy(tmp_path, text))
    assert policy.environments["prod"].production is True


def test_name_production_with_active_is_refused(tmp_path: Path) -> None:
    """Naming it production and believing you means the active check fires."""
    text = "version: 1\nenvironments:\n  production: { tiers: [static, active] }\n"
    with pytest.raises(env.EnvironmentConfigError, match="active"):
        env.load_policy(_write_policy(tmp_path, text))


def test_unsupported_version_is_refused(tmp_path: Path) -> None:
    text = "version: 2\nenvironments:\n  dev: { tiers: [static] }\n"
    with pytest.raises(env.EnvironmentConfigError, match="version"):
        env.load_policy(_write_policy(tmp_path, text))


def test_binding_naming_an_undefined_environment_errors(tmp_path: Path) -> None:
    _write_policy(tmp_path)
    with pytest.raises(env.EnvironmentConfigError, match="ghost"):
        env.resolve(tmp_path, override="ghost")


# --------------------------------------------------------------------------
# Resolution: precedence, and the allowed tiers per environment
# --------------------------------------------------------------------------


def test_flag_beats_the_binding_file(tmp_path: Path) -> None:
    _write_policy(tmp_path)
    _write_binding(tmp_path, "prod")
    resolution = env.resolve(tmp_path, override="dev")
    assert resolution.environment == "dev"
    assert resolution.allows("active")


def test_binding_file_is_used_when_no_flag(tmp_path: Path) -> None:
    _write_policy(tmp_path)
    _write_binding(tmp_path, "prod")
    resolution = env.resolve(tmp_path)
    assert resolution.environment == "prod"
    assert resolution.allows("static")
    assert resolution.allows("readonly")
    assert not resolution.allows("active")
    assert resolution.production is True


def test_dev_allows_the_active_tier(tmp_path: Path) -> None:
    _write_policy(tmp_path)
    _write_binding(tmp_path, "dev")
    resolution = env.resolve(tmp_path)
    assert resolution.allowed_tiers == ("static", "readonly", "active")
    assert resolution.production is False


# --------------------------------------------------------------------------
# Absence semantics: the safe floor
# --------------------------------------------------------------------------


def test_no_policy_file_is_the_safe_floor_and_not_on_safe_floor_notice(
    tmp_path: Path,
) -> None:
    """No policy at all: static + readonly run, and nothing is announced."""
    resolution = env.resolve(tmp_path)
    assert resolution.policy_present is False
    assert resolution.environment is None
    assert resolution.allowed_tiers == env.SAFE_FLOOR_TIERS
    assert not resolution.allows("active")
    # No policy means no configuration to speak of, so there is no notice.
    assert resolution.on_safe_floor is False


def test_policy_present_but_no_binding_is_the_announced_safe_floor(
    tmp_path: Path,
) -> None:
    _write_policy(tmp_path)
    resolution = env.resolve(tmp_path)
    assert resolution.policy_present is True
    assert resolution.environment is None
    assert resolution.allowed_tiers == env.SAFE_FLOOR_TIERS
    assert not resolution.allows("active")
    # A committed policy with nothing bound is the one case worth announcing.
    assert resolution.on_safe_floor is True
