import ast
import sys
from pathlib import Path

import pytest

import gh_pool
import gh_pool.client

BASE = Path(gh_pool.__file__).parent
BASE_MODULES = sorted(p for p in BASE.glob("*.py") if p.name != "__init__.py")


def imported_names(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.add(node.module)
    return found


def offenders(path: Path) -> set[str]:
    bad: set[str] = set()
    for name in imported_names(path):
        root = name.partition(".")[0]
        if root in sys.stdlib_module_names:
            continue
        if root != "gh_pool":
            bad.add(name)
            continue
        rest = name.partition(".")[2]
        if rest and not (BASE / f"{rest.replace('.', '/')}.py").exists():
            bad.add(name)
    return bad


@pytest.mark.parametrize(
    "path", [BASE / "__init__.py", *BASE_MODULES], ids=lambda p: p.name
)
def test_the_base_package_imports_only_the_standard_library(path):
    assert offenders(path) == set()


def test_every_public_client_name_resolves():
    missing = [
        name for name in gh_pool.client.__all__ if not hasattr(gh_pool.client, name)
    ]
    assert missing == []


def test_the_client_export_list_is_sorted():
    assert gh_pool.client.__all__ == sorted(gh_pool.client.__all__)


def test_the_package_ships_a_typing_marker():
    assert (BASE / "py.typed").is_file()
