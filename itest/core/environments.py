"""Environment profiles: a committed tier policy and a local binding.

Two artifacts, deliberately split by whether they belong to the project or to
the checkout:

- **The policy** (`.itest/environments.yaml`, committed and code-reviewed) names
  environments and, per environment, which test tiers may run there. It is the
  place a reviewer can veto an active (mutating) tier before it ever exists.
- **The binding** (a ``--environment`` flag, else the local `.itest/environment`
  file) says which of those environments THIS checkout is pointed at. It is
  machine-local state, gitignored like ``skill-answers.yaml``.

An active-tier test runs only when the policy allows it *and* the binding
selects an environment that allows it. Any absence — no policy, no binding, an
environment whose tiers omit ``active`` — resolves to the **safe floor**:
static and readonly only. Absence is never permission.

Every policy problem is a hard error raised at load time, not at run time, so a
policy that would let a mutating test loose in production cannot be committed
quietly against a green suite: ``itest verify`` refuses to start.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from itest.core.manifest import Tier
from itest.core.planner import ITEST_DIR

#: The tiers ITest knows, in escalating order of blast radius. Mirrors
#: ``manifest.Tier`` — a policy naming anything else is a typo, not a tier.
VALID_TIERS: tuple[Tier, ...] = ("static", "readonly", "active")

#: What runs when nothing grants more: no AWS mutation, describe/get at most.
SAFE_FLOOR_TIERS: tuple[Tier, ...] = ("static", "readonly")

#: Names that mean production even when ``production:`` is not written out.
#: Name an environment one of these and ITest believes you — the active-tier
#: refusal then applies whether or not the flag was set.
PRODUCTION_NAMES: frozenset[str] = frozenset({"prod", "production"})

#: The only policy schema this build understands.
POLICY_VERSION = 1

ENVIRONMENTS_NAME = "environments.yaml"
BINDING_NAME = "environment"


class EnvironmentConfigError(Exception):
    """A policy or binding problem. Actionable, and raised at load time."""


def policy_path(base_dir: Path) -> Path:
    return base_dir / ITEST_DIR / ENVIRONMENTS_NAME


def binding_path(base_dir: Path) -> Path:
    return base_dir / ITEST_DIR / BINDING_NAME


class Environment(BaseModel):
    """One named environment and the tiers it may run."""

    name: str
    tiers: list[Tier] = Field(default_factory=list)
    production: bool = False


class EnvironmentPolicy(BaseModel):
    """The committed policy: a version and the environments it defines."""

    version: int = POLICY_VERSION
    environments: dict[str, Environment] = Field(default_factory=dict)


@dataclass(frozen=True)
class Resolution:
    """The environment this run resolved to, and what it may run.

    ``environment`` is ``None`` on the safe floor — either no policy exists or
    a policy exists with nothing bound. ``on_safe_floor`` distinguishes the
    second case (worth announcing) from the first (byte-identical to a project
    that never heard of environments).
    """

    allowed_tiers: tuple[Tier, ...]
    environment: str | None
    production: bool
    policy_present: bool

    def allows(self, tier: str) -> bool:
        return tier in self.allowed_tiers

    @property
    def on_safe_floor(self) -> bool:
        """True when a policy exists but nothing is bound to it."""
        return self.policy_present and self.environment is None

    @property
    def gated_tag(self) -> str:
        """The bracketed tag a gated point carries, e.g. ``GATED prod``.

        On the safe floor there is no environment name to show, so it is the
        bare ``GATED``.
        """
        return f"GATED {self.environment}" if self.environment else "GATED"


def load_policy(path: str | Path) -> EnvironmentPolicy:
    """Load and validate the policy at ``path``.

    Validation is exhaustive and up front: an unsupported version, an unknown
    tier, or a production environment that lists ``active`` each raises
    ``EnvironmentConfigError`` here, before any test is collected.
    """
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise EnvironmentConfigError(f"{path} is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise EnvironmentConfigError(
            f"{path} must be a mapping with 'version' and 'environments' keys."
        )

    version = raw.get("version")
    if version != POLICY_VERSION:
        raise EnvironmentConfigError(
            f"{path} declares version {version!r}, but this build understands "
            f"only version {POLICY_VERSION}. Upgrade ITest or fix the file."
        )

    raw_envs = raw.get("environments")
    if not isinstance(raw_envs, dict) or not raw_envs:
        raise EnvironmentConfigError(
            f"{path} has no 'environments' section. Define at least one, e.g. "
            "`environments: {dev: {tiers: [static, readonly]}}`."
        )

    environments: dict[str, Environment] = {}
    for name, spec in raw_envs.items():
        if spec is None:
            spec = {}
        if not isinstance(spec, dict):
            # `dev: readonly` or `dev: [static]` — a scalar/list where a mapping
            # is required. A config error at load, not an AttributeError at run.
            raise EnvironmentConfigError(
                f"Environment '{name}' must be a mapping, e.g. "
                "`{tiers: [static, readonly]}`; got "
                f"{type(spec).__name__} ({spec!r})."
            )
        tiers = list(spec.get("tiers", []))
        production = bool(spec.get("production", False)) or name in PRODUCTION_NAMES

        for tier in tiers:
            if tier not in VALID_TIERS:
                raise EnvironmentConfigError(
                    f"Environment '{name}' lists unknown tier '{tier}'. "
                    f"Valid tiers: {', '.join(VALID_TIERS)}."
                )
        # Refused here, not at run time: a production environment that could
        # run mutating tests must never make it past code review green.
        if production and "active" in tiers:
            raise EnvironmentConfigError(
                f"Environment '{name}' is production but lists the 'active' "
                "tier. Production must not run mutating tests: drop 'active' "
                "from its tiers, or drop its production status."
            )
        environments[name] = Environment(name=name, tiers=tiers, production=production)

    return EnvironmentPolicy(version=version, environments=environments)


def _read_binding(base_dir: Path) -> str | None:
    """The environment named by the local binding file, or ``None``."""
    path = binding_path(base_dir)
    if not path.exists():
        return None
    name = path.read_text(encoding="utf-8").strip()
    return name or None


def resolve(base_dir: Path, override: str | None = None) -> Resolution:
    """Resolve the environment for a run rooted at ``base_dir``.

    Precedence for the binding: an explicit ``override`` (the ``--environment``
    flag) beats the local `.itest/environment` file. With a policy present but
    nothing bound, or with no policy at all, the result is the safe floor.
    """
    policy_file = policy_path(base_dir)
    if not policy_file.exists():
        # No policy: exactly today's behavior for static/readonly, and active
        # withheld because absence of configuration is never permission.
        return Resolution(
            allowed_tiers=SAFE_FLOOR_TIERS,
            environment=None,
            production=False,
            policy_present=False,
        )

    policy = load_policy(policy_file)
    name = override if override is not None else _read_binding(base_dir)

    if name is None:
        return Resolution(
            allowed_tiers=SAFE_FLOOR_TIERS,
            environment=None,
            production=False,
            policy_present=True,
        )

    environment = policy.environments.get(name)
    if environment is None:
        defined = ", ".join(sorted(policy.environments)) or "(none)"
        raise EnvironmentConfigError(
            f"Bound environment '{name}' is not defined in {policy_file}. "
            f"Defined environments: {defined}."
        )

    return Resolution(
        allowed_tiers=tuple(environment.tiers),
        environment=name,
        production=environment.production,
        policy_present=True,
    )
