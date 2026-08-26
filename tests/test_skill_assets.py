"""The bundled agent skill ships in this repo, so its shape is tested.

These are asset tests, not behaviour tests: they guard the contract another
agent relies on — that the files are where SKILL.md says, that the frontmatter
parses, that the conftest template it tells the agent to copy is real
extractable Python rather than prose that merely looks like code, and that
every point type ITest can emit has a recipe to implement it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from itest.core.detectors.base import detect_all

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / "skills" / "itest-implementer"
SKILL_MD = SKILL_DIR / "SKILL.md"
RECIPE_DIR = SKILL_DIR / "references" / "recipes"
CONFTEST_MD = SKILL_DIR / "references" / "conftest.md"

SG_RECIPE = RECIPE_DIR / "sg_edge.md"
IAM_RECIPE = RECIPE_DIR / "iam_edge.md"
EVENT_RECIPE = RECIPE_DIR / "event_edge.md"

TEMPLATE_BEGIN = "<!-- BEGIN conftest.py -->"
TEMPLATE_END = "<!-- END conftest.py -->"

FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures"


def parse_frontmatter(path: Path) -> dict:
    """Return the YAML frontmatter block of a markdown file."""
    import yaml

    match = FRONTMATTER_RE.match(path.read_text(encoding="utf-8"))
    assert match is not None, f"{path.name} has no YAML frontmatter block"
    return yaml.safe_load(match.group(1)) or {}


def extract_conftest_template(path: Path = CONFTEST_MD) -> str:
    """Return the conftest.py template from its marked fence."""
    text = path.read_text(encoding="utf-8")
    assert TEMPLATE_BEGIN in text, f"{path.name} is missing {TEMPLATE_BEGIN}"
    assert TEMPLATE_END in text, f"{path.name} is missing {TEMPLATE_END}"

    start = text.index(TEMPLATE_BEGIN) + len(TEMPLATE_BEGIN)
    block = text[start : text.index(TEMPLATE_END)].strip()
    assert block.startswith("```python"), "template must be a ```python fence"
    return block.split("\n", 1)[1].rsplit("```", 1)[0]


def emitted_point_types() -> set[str]:
    """Every point type the registered detectors actually produce.

    Derived by running detection over the checked-in fixtures rather than from
    a hand-kept list, so a new detector cannot quietly ship without a recipe.
    """
    types: set[str] = set()
    for path in sorted(FIXTURE_DIR.rglob("*.json")):
        points, _ = detect_all(json.loads(path.read_text(encoding="utf-8")))
        types |= {p.type for p in points}
    return types


# --------------------------------------------------------------------------
# Files and frontmatter
# --------------------------------------------------------------------------


def test_skill_files_exist() -> None:
    assert SKILL_MD.is_file(), f"missing {SKILL_MD}"
    assert CONFTEST_MD.is_file(), f"missing {CONFTEST_MD}"
    for recipe in (SG_RECIPE, IAM_RECIPE, EVENT_RECIPE):
        assert recipe.is_file(), f"missing {recipe}"


def test_skill_frontmatter_parses() -> None:
    data = parse_frontmatter(SKILL_MD)
    assert data.get("name") == "itest-implementer"
    description = str(data.get("description", "")).strip()
    assert description, "frontmatter must carry a description"


# --------------------------------------------------------------------------
# Recipe coverage
# --------------------------------------------------------------------------


def test_every_emitted_point_type_has_a_recipe() -> None:
    """A detector without a recipe silently loses coverage for its points."""
    emitted = emitted_point_types()
    assert emitted, "fixtures produced no points; detection may be broken"

    recipes = {p.stem for p in RECIPE_DIR.glob("*.md")}
    missing = emitted - recipes
    assert not missing, f"point types with no recipe: {sorted(missing)}"


def test_no_recipe_without_a_detector() -> None:
    """The reverse drift: a recipe for a type nothing emits is dead weight."""
    recipes = {p.stem for p in RECIPE_DIR.glob("*.md")}
    assert recipes <= emitted_point_types()


@pytest.mark.parametrize(
    "recipe,heading",
    [
        (SG_RECIPE, "sg_edge"),
        (IAM_RECIPE, "iam_edge"),
        (EVENT_RECIPE, "event_edge"),
    ],
)
def test_recipe_names_its_type_and_explains_failure(recipe: Path, heading: str) -> None:
    text = recipe.read_text(encoding="utf-8")
    assert heading in text
    # Every recipe must tell the agent how to read a red result, or it will
    # treat drift as a bug in its own assertion and weaken it.
    assert "failure" in text.lower()


@pytest.mark.parametrize("field", ["source", "target", "hcl_address"])
def test_recipes_document_point_fields(field: str) -> None:
    for recipe in (SG_RECIPE, IAM_RECIPE, EVENT_RECIPE):
        assert field in recipe.read_text(encoding="utf-8"), f"{recipe.name}"


def test_recipes_point_at_the_shared_conftest() -> None:
    """One resolver, referenced everywhere, so the recipes cannot diverge."""
    for recipe in (SG_RECIPE, IAM_RECIPE, EVENT_RECIPE):
        text = recipe.read_text(encoding="utf-8")
        assert "conftest.md" in text, f"{recipe.name} does not reference the template"
        assert TEMPLATE_BEGIN not in text, (
            f"{recipe.name} carries its own copy of the conftest template"
        )


# --------------------------------------------------------------------------
# The conftest template
# --------------------------------------------------------------------------


def test_conftest_template_extracts_and_compiles() -> None:
    source = extract_conftest_template()
    assert "def resolve_address(" in source, "template must define resolve_address"
    # Compiles as real Python — the agent copies this verbatim.
    compile(source, "conftest.py", "exec")


def test_conftest_template_reads_config_rather_than_hardcoding_it() -> None:
    """The template must take account details from the answers file."""
    source = extract_conftest_template()
    assert ".itest/skill-answers.yaml" in source
    for key in ("terraform_dir", "aws_profile", "aws_region"):
        assert key in source, f"template never reads {key}"


@pytest.mark.parametrize("fixture", ["ec2", "iam", "lambda_", "sqs"])
def test_conftest_template_provides_clients_for_every_recipe(fixture: str) -> None:
    """Each recipe's assertions need its service client to exist."""
    source = extract_conftest_template()
    assert f"def {fixture}(" in source, f"template has no {fixture} fixture"


def test_conftest_template_is_read_only() -> None:
    """Guardrail: generated checks must never mutate AWS."""
    source = extract_conftest_template()
    for forbidden in ("create_", "delete_", "put_", "update_", "terraform apply"):
        assert forbidden not in source, f"template contains {forbidden}"


def test_every_recipe_example_is_valid_python() -> None:
    """The agent copies these blocks verbatim; a fragment that will not parse
    is a broken instruction, not an illustration."""
    fence = re.compile(r"```python\n(.*?)```", re.DOTALL)
    for recipe in sorted(RECIPE_DIR.glob("*.md")):
        blocks = fence.findall(recipe.read_text(encoding="utf-8"))
        assert blocks, f"{recipe.name} shows no python example"
        for index, block in enumerate(blocks):
            compile(block, f"{recipe.name}#{index}", "exec")
