"""Load, validate and resolve the declarations under ``.itest/tools/``.

One file per server, named for it: ``.itest/tools/<server>.yaml``. The loader's
job is to turn those files into validated :class:`~.schema.Declaration` objects
and to refuse, loudly and with the offending name in the message, everything it
cannot turn into one:

- a file whose name is not its ``server`` (the path is the address);
- any unknown key, at any level (a typo is not a fact);
- a URL or a token written where an environment variable's NAME belongs — and
  the refusal never repeats the value;
- an ``environments.active_allowed_in`` entry that the committed
  ``.itest/environments.yaml`` does not already permit the ``active`` tier in.
  A declaration may narrow the policy and never widen it, because the policy is
  the code-reviewed artifact; absence of a policy is not permission.

Resolution is separate from loading. :func:`resolve_url` reads the url's
environment variable through the same ``resolve_credential`` path a token takes
— shell first, then a gitignored ``.itest/.env`` — and returns ``None`` when it
is unset, which the caller reports as *unreachable* rather than crashing on.
Nothing here logs, stores, or echoes a resolved URL.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import ValidationError

from itest.core import environments as environment_policy
from itest.core.declarations.schema import Declaration
from itest.core.planner import ITEST_DIR
from itest.probes.credential import resolve_credential

#: Where declarations live, relative to the project root.
TOOLS_DIR = "tools"


class DeclarationError(Exception):
    """A declaration that cannot be used. Names the file and the offending key.

    Never carries a resolved URL or a credential: the message is read in a
    terminal, a CI log and a pasted issue.
    """


def declarations_dir(base_dir: Path) -> Path:
    return base_dir / ITEST_DIR / TOOLS_DIR


def declaration_path(server: str) -> str:
    """The repo-relative path a server's declaration must live at."""
    return f"{ITEST_DIR}/{TOOLS_DIR}/{server}.yaml"


def _format_validation_error(path: Path, exc: ValidationError) -> str:
    """Render a pydantic error WITHOUT its input values.

    Pydantic's own ``str(exc)`` quotes the offending input, which is exactly the
    URL or token this schema exists to keep out of messages. Only the location
    and the explanation are repeated.
    """
    lines = [f"{path} is not a valid declaration:"]
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "(document)"
        lines.append(f"  {location}: {error['msg']}")
    return "\n".join(lines)


#: A key at the start of a YAML line, optionally as a sequence item.
_LINE_KEY = re.compile(r"^\s*(?:-\s+)?([A-Za-z_][A-Za-z0-9_-]*)\s*:")
#: A quoted fragment inside PyYAML's problem text (a character, an alias).
_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")


def _format_yaml_error(path: Path, text: str, exc: yaml.YAMLError) -> str:
    """Render a YAML syntax error WITHOUT the source it points at.

    PyYAML's ``str(exc)`` quotes the offending line with a caret under it, and
    that line is where a pasted URL or token would be. Only the line number,
    the key on that line, and the kind of problem are repeated.
    """
    mark = getattr(exc, "problem_mark", None)
    if mark is None:
        return f"{path} is not valid YAML."
    where = f"line {mark.line + 1}"
    lines = text.splitlines()
    if mark.line < len(lines):
        key = _LINE_KEY.match(lines[mark.line])
        if key:
            where += f", key '{key.group(1)}'"
    problem = _QUOTED.sub("(...)", getattr(exc, "problem", None) or "").strip()
    detail = f": {problem}" if problem else ""
    return (
        f"{path} is not valid YAML ({where}){detail}. The line itself is not "
        "repeated, so no value in it reaches this message."
    )


def _load_one(path: Path) -> Declaration:
    """Parse and validate one declaration file."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DeclarationError(f"{path} could not be read: {exc}") from exc
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        # Not chained: the original exception's text is the leak.
        raise DeclarationError(_format_yaml_error(path, text, exc)) from None

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise DeclarationError(
            f"{path} must be a mapping with a 'server' key; got {type(raw).__name__}."
        )

    try:
        declaration = Declaration.model_validate(raw)
    except ValidationError as exc:
        raise DeclarationError(_format_validation_error(path, exc)) from None

    if declaration.server != path.stem:
        raise DeclarationError(
            f"{path} declares server '{declaration.server}' but its file name "
            f"says '{path.stem}'. The path is the address: a declaration lives "
            f"at {declaration_path(declaration.server)}. Rename one of them."
        )
    return declaration


def _check_active_environments(declaration: Declaration, base_dir: Path) -> None:
    """Refuse an ``active_allowed_in`` the committed policy does not support.

    Raised at load time for the same reason the policy's own problems are: a
    declaration that would let a mutating check run where the project forbids it
    must not survive a green test run.
    """
    wanted = declaration.environments.active_allowed_in
    if not wanted:
        return

    policy_file = environment_policy.policy_path(base_dir)
    path = declaration_path(declaration.server)
    if not policy_file.exists():
        raise DeclarationError(
            f"{path} allows the active tier in {wanted[0]!r}, but there is no "
            f"{policy_file}. No environment permits the active tier until the "
            "committed policy says so — absence of a policy is not permission."
        )

    try:
        policy = environment_policy.load_policy(policy_file)
    except environment_policy.EnvironmentConfigError as exc:
        raise DeclarationError(
            f"{path} names active environments, but {policy_file} cannot be read: {exc}"
        ) from None

    allowed = sorted(
        name
        for name, environment in policy.environments.items()
        if "active" in environment.tiers
    )
    for name in wanted:
        environment = policy.environments.get(name)
        if environment is None:
            raise DeclarationError(
                f"{path} allows the active tier in {name!r}, which is not "
                f"defined in {policy_file}. Defined environments: "
                f"{', '.join(sorted(policy.environments)) or '(none)'}."
            )
        if "active" not in environment.tiers:
            raise DeclarationError(
                f"{path} allows the active tier in {name!r}, but {policy_file} "
                f"does not permit the active tier there (it allows: "
                f"{', '.join(environment.tiers) or '(none)'}). A declaration "
                "can narrow the committed policy, never widen it. Environments "
                f"that do permit active: {', '.join(allowed) or '(none)'}."
            )


def load_declarations(base_dir: Path) -> list[Declaration]:
    """Every declaration under ``<base_dir>/.itest/tools/``, by file name.

    Returns an empty list when the directory does not exist: a project that has
    never declared a server behaves exactly as it did before declarations
    existed.
    """
    directory = declarations_dir(base_dir)
    if not directory.is_dir():
        return []

    declarations: list[Declaration] = []
    for path in sorted(directory.glob("*.yaml")):
        declaration = _load_one(path)
        _check_active_environments(declaration, base_dir)
        declarations.append(declaration)
    return declarations


def resolve_url(declaration: Declaration, base_dir: Path | None = None) -> str | None:
    """The server's base URL, read from the variable the declaration names.

    ``None`` when the declaration names no ``url_env`` or the variable is unset
    or empty. Never raises, never logs: an unreachable server is a line in the
    plan, not a crash.
    """
    url_env = declaration.transport.url_env
    if not url_env:
        return None
    return resolve_credential(url_env, base_dir)


def missing_url_message(declaration: Declaration) -> str:
    """What plan prints for a server whose url variable is not set.

    The NAME is safe to print and is the whole reason for naming one.
    """
    return f"unreachable: {declaration.transport.url_env} not set"
