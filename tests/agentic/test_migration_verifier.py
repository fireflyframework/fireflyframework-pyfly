"""Completeness gates must reject omissions, including non-Python resources."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def verifier() -> Any:
    path = Path(__file__).parents[2] / "scripts" / "verify_agentic_migration.py"
    spec = importlib.util.spec_from_file_location("agentic_migration_verifier", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def migration(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    package = tmp_path / "src/pyfly/agentic"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('from .api import Agent\n__all__ = ["Agent"]\n')
    (package / "api.py").write_text("class Agent:\n    pass\n")
    (package / "py.typed").touch()
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "pyfly"\ndependencies = []\n'
        '[project.optional-dependencies]\nagentic = ["pydantic-ai==2.55.0"]\n'
        '[project.entry-points."pyfly.auto_configuration"]\n'
        'agentic = "pyfly.agentic.auto_configuration:AgenticAutoConfiguration"\n'
    )
    return tmp_path, {
        "files": [
            {"source": "old/__init__.py", "target": "src/pyfly/agentic/__init__.py", "kind": "module"},
            {"source": "old/api.py", "target": "src/pyfly/agentic/api.py", "kind": "module"},
            {"source": "old/py.typed", "target": "src/pyfly/agentic/py.typed", "kind": "resource"},
        ],
        "modules": [
            {"target": "src/pyfly/agentic/__init__.py", "exports": ["Agent"], "declarations": []},
            {"target": "src/pyfly/agentic/api.py", "exports": [], "declarations": ["Agent"]},
        ],
        "extras": {"agentic": ["pydantic-ai==2.55.0"]},
        "renames": {"FireflyAgent": "Agent"},
    }


def test_complete_fixture_passes(verifier: Any, migration: tuple[Path, dict[str, Any]]) -> None:
    root, manifest = migration
    assert verifier.verify(root, manifest) == []


@pytest.mark.parametrize("name", ["api.py", "py.typed"])
def test_dropped_module_or_resource_is_rejected(
    verifier: Any, migration: tuple[Path, dict[str, Any]], name: str
) -> None:
    root, manifest = migration
    (root / "src/pyfly/agentic" / name).unlink()
    assert any(f"Missing target: src/pyfly/agentic/{name}" in error for error in verifier.verify(root, manifest))


def test_dropped_public_export_is_rejected(verifier: Any, migration: tuple[Path, dict[str, Any]]) -> None:
    root, manifest = migration
    (root / "src/pyfly/agentic/__init__.py").write_text("from .api import Agent\n__all__ = []\n")
    assert any("Missing exports" in error for error in verifier.verify(root, manifest))


def test_dropped_public_declaration_is_rejected(verifier: Any, migration: tuple[Path, dict[str, Any]]) -> None:
    root, manifest = migration
    (root / "src/pyfly/agentic/api.py").write_text("value = 1\n")
    assert any("Missing declarations" in error for error in verifier.verify(root, manifest))


def test_dropped_native_import_is_rejected(verifier: Any, migration: tuple[Path, dict[str, Any]]) -> None:
    root, manifest = migration
    path = root / "src/pyfly/agentic/api.py"
    path.write_text(path.read_text() + "from pyfly.agentic.missing import Missing\n")
    assert any("Missing native import module" in error for error in verifier.verify(root, manifest))


def test_incomplete_manifest_is_rejected(verifier: Any, migration: tuple[Path, dict[str, Any]]) -> None:
    root, manifest = migration
    manifest["summary"] = {"tracked_source_files": 4, "python_source_files": 2}
    assert any("Manifest source-file count" in error for error in verifier.verify(root, manifest))


def test_stale_export_binding_is_rejected(verifier: Any, migration: tuple[Path, dict[str, Any]]) -> None:
    root, manifest = migration
    (root / "src/pyfly/agentic/__init__.py").write_text('__all__ = ["Agent"]\n')
    assert any("Unbound export" in error for error in verifier.verify(root, manifest))


def test_pep695_public_type_alias_is_a_valid_binding(verifier: Any, migration: tuple[Path, dict[str, Any]]) -> None:
    root, manifest = migration
    (root / "src/pyfly/agentic/__init__.py").write_text('type Agent = str\n__all__ = ["Agent"]\n')
    assert verifier.verify(root, manifest) == []


@pytest.mark.parametrize("code", ["FireflyAgent = Agent\n", "import fireflyframework_agentic\n"])
def test_legacy_runtime_alias_or_import_is_rejected(
    verifier: Any, migration: tuple[Path, dict[str, Any]], code: str
) -> None:
    root, manifest = migration
    path = root / "src/pyfly/agentic/api.py"
    path.write_text(path.read_text() + code)
    assert any("Legacy" in error for error in verifier.verify(root, manifest))


def test_dropped_extra_dependency_is_rejected(verifier: Any, migration: tuple[Path, dict[str, Any]]) -> None:
    root, manifest = migration
    path = root / "pyproject.toml"
    path.write_text(path.read_text().replace('agentic = ["pydantic-ai==2.55.0"]', "agentic = []"))
    assert any("Extra dependencies changed" in error for error in verifier.verify(root, manifest))


def test_framework_data_extra_drift_is_rejected(verifier: Any, migration: tuple[Path, dict[str, Any]]) -> None:
    root, manifest = migration
    manifest["supporting_extras"] = {"data-relational": ["sqlalchemy[asyncio]>=2.0.50"]}
    assert any("Extra dependencies changed: data-relational" in error for error in verifier.verify(root, manifest))


def test_unaccounted_original_dependency_translation_is_rejected(
    verifier: Any, migration: tuple[Path, dict[str, Any]]
) -> None:
    root, manifest = migration
    manifest["source_optional_dependencies"] = {"mongodb": ["motor>=3.6.0"]}
    manifest["dependency_translations"] = {}
    assert any("Missing dependency translation: mongodb" in error for error in verifier.verify(root, manifest))
