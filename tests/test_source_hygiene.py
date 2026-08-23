"""Structural checks over the source itself.

Written after a duplicated edit landed the same setting twice in one dict, the
same field twice in one dataclass, and a helper that shadowed the import above
it. Python accepts all three silently — the later one simply wins — so the only
symptom was a test asserting the behaviour of code that was no longer running.

Cheap to run, and it catches a class of mistake that review does not.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("andromeda_cli", "andromeda_agent", "andromeda_tools", "andromeda_tui")


def source_files() -> list[Path]:
    files: list[Path] = []
    for package in PACKAGES:
        files.extend(sorted((PACKAGE_ROOT / package).rglob("*.py")))
    return files


@pytest.fixture(scope="module")
def parsed() -> list[tuple[Path, ast.Module]]:
    out = []
    for path in source_files():
        out.append((path, ast.parse(path.read_text(encoding="utf-8"))))
    return out


def test_there_are_source_files_to_check(parsed):
    """Otherwise every check below passes vacuously."""
    assert len(parsed) > 20


def test_no_dict_literal_repeats_a_key(parsed):
    """A repeated key is silently the later value — including in DEFAULTS."""
    offences = []
    for path, tree in parsed:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
            for key in sorted({k for k in keys if keys.count(k) > 1}):
                offences.append(f"{path.name}:{node.lineno} repeats {key!r}")
    assert offences == [], "\n".join(offences)


def test_no_scope_defines_the_same_name_twice(parsed):
    """Two `def`s of one name, or a class defined twice, is one dead body."""
    offences = []
    for path, tree in parsed:
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Module, ast.ClassDef)):
                continue
            names = [
                child.name
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            ]
            for name in sorted({n for n in names if names.count(n) > 1}):
                offences.append(f"{path.name}: defines {name} twice")
    assert offences == [], "\n".join(offences)


def test_no_dataclass_declares_a_field_twice(parsed):
    offences = []
    for path, tree in parsed:
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Module, ast.ClassDef)):
                continue
            fields = [
                child.target.id
                for child in node.body
                if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name)
            ]
            for name in sorted({f for f in fields if fields.count(f) > 1}):
                offences.append(f"{path.name}: declares {name} twice")
    assert offences == [], "\n".join(offences)


def test_no_module_shadows_a_name_it_imported(parsed):
    """The one that actually shipped: `base.py` imported `reasoning_for` from
    `models` and then defined its own, so every call took the local version."""
    offences = []
    for path, tree in parsed:
        imported: set[str] = set()
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    imported.add(alias.asname or alias.name.split(".")[0])
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name in imported:
                    offences.append(f"{path.name}: {node.name} shadows an import")
    assert offences == [], "\n".join(offences)


def test_every_source_file_parses(parsed):
    assert all(tree is not None for _, tree in parsed)
