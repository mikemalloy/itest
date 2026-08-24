"""The bundled agent skill ships in this repo, so its shape is tested.

These are asset tests, not behaviour tests: they guard the contract another
agent relies on — that the files are where SKILL.md says, that the frontmatter
parses, and that the conftest template it tells the agent to copy is real,
extractable Python rather than prose that merely looks like code.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / "skills" / "itest-implementer"
SKILL_MD = SKILL_DIR / "SKILL.md"
SG_RECIPE = SKILL_DIR / "references" / "recipes" / "sg_edge.md"

TEMPLATE_BEGIN = "<!-- BEGIN conftest.py -->"
TEMPLATE_END = "<!-- END conftest.py -->"

FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def parse_frontmatter(path: Path) -> dict:
    """Return the YAML frontmatter block of a markdown file."""
    match = FRONTMATTER_RE.match(path.read_text(encoding="utf-8"))
    assert match is not None, f"{path.name} has no YAML frontmatter block"
    return yaml.safe_load(match.group(1)) or {}


def extract_conftest_template(path: Path) -> str:
    """Return the conftest.py template from the recipe's marked fence."""
    text = path.read_text(encoding="utf-8")
    assert TEMPLATE_BEGIN in text, f"{path.name} is missing {TEMPLATE_BEGIN}"
    assert TEMPLATE_END in text, f"{path.name} is missing {TEMPLATE_END}"

    start = text.index(TEMPLATE_BEGIN) + len(TEMPLATE_BEGIN)
    block = text[start : text.index(TEMPLATE_END)].strip()
    assert block.startswith("```python"), "template must be a ```python fence"
    return block.split("\n", 1)[1].rsplit("```", 1)[0]


def test_skill_files_exist() -> None:
    assert SKILL_MD.is_file(), f"missing {SKILL_MD}"
    assert SG_RECIPE.is_file(), f"missing {SG_RECIPE}"


def test_skill_frontmatter_parses() -> None:
    data = parse_frontmatter(SKILL_MD)
    assert data.get("name") == "itest-implementer"
    description = str(data.get("description", "")).strip()
    assert description, "frontmatter must carry a description"


def test_conftest_template_extracts_and_compiles() -> None:
    source = extract_conftest_template(SG_RECIPE)
    assert "def resolve_address(" in source, "template must define resolve_address"
    # Compiles as real Python — the agent copies this verbatim.
    compile(source, "conftest.py", "exec")


def test_conftest_template_reads_config_rather_than_hardcoding_it() -> None:
    """The template must take account details from the answers file."""
    source = extract_conftest_template(SG_RECIPE)
    assert ".itest/skill-answers.yaml" in source
    for key in ("terraform_dir", "aws_profile", "aws_region"):
        assert key in source, f"template never reads {key}"


@pytest.mark.parametrize("field", ["source", "target", "hcl_address"])
def test_recipe_documents_point_fields(field: str) -> None:
    assert field in SG_RECIPE.read_text(encoding="utf-8")
