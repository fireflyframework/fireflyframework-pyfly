"""Distribution checks inspect actual archive contents, not build exit codes."""

from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def checker() -> Any:
    path = Path(__file__).parents[2] / "scripts/check_agentic_distribution.py"
    spec = importlib.util.spec_from_file_location("agentic_distribution_checker", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def source(tmp_path: Path) -> Path:
    package = tmp_path / "src/pyfly/agentic"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("value = 1\n")
    (package / "py.typed").touch()
    return tmp_path


def wheel(source: Path, files: dict[str, bytes]) -> Path:
    path = source / "candidate.whl"
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return path


def test_complete_archive_matches_native_source(checker: Any, source: Path) -> None:
    path = wheel(source, {"pyfly/agentic/__init__.py": b"value = 1\n", "pyfly/agentic/py.typed": b""})
    result = checker.inspect_archive(path, source, {"files": []})
    assert result["agentic_runtime_files"] == 2


def test_wheel_missing_typed_resource_is_rejected(checker: Any, source: Path) -> None:
    path = wheel(source, {"pyfly/agentic/__init__.py": b"value = 1\n"})
    with pytest.raises(ValueError, match="Missing required archive files.*py.typed"):
        checker.inspect_archive(path, source, {"files": []})


@pytest.mark.parametrize("name", [".venv313/bin/python", "book/release-qa/preview.png", "src/pyfly/__pycache__/x.pyc"])
def test_generated_environment_cache_and_qa_files_are_rejected(checker: Any, source: Path, name: str) -> None:
    path = wheel(source, {"pyfly/agentic/__init__.py": b"value = 1\n", "pyfly/agentic/py.typed": b"", name: b"junk"})
    with pytest.raises(ValueError, match="Generated environment/cache/QA files"):
        checker.inspect_archive(path, source, {"files": []})


def test_stale_runtime_bytes_are_rejected(checker: Any, source: Path) -> None:
    path = wheel(source, {"pyfly/agentic/__init__.py": b"value = 0\n", "pyfly/agentic/py.typed": b""})
    with pytest.raises(ValueError, match="Archived runtime differs"):
        checker.inspect_archive(path, source, {"files": []})


def test_missing_optional_dependency_metadata_is_rejected(checker: Any, source: Path) -> None:
    path = wheel(
        source,
        {
            "pyfly/agentic/__init__.py": b"value = 1\n",
            "pyfly/agentic/py.typed": b"",
            "pyfly-1.dist-info/METADATA": b"Metadata-Version: 2.4\nName: pyfly\nProvides-Extra: agentic\n",
        },
    )
    with pytest.raises(ValueError, match="Optional dependency metadata differs"):
        checker.inspect_archive(path, source, {"files": [], "extras": {"agentic": ["pydantic-ai==2.55.0"]}})


def test_hatch_flattened_self_references_preserve_dependency_contract(checker: Any) -> None:
    content = {
        "pyfly-1.dist-info/METADATA": (
            b"Metadata-Version: 2.4\nName: pyfly\n"
            b"Provides-Extra: agentic\nProvides-Extra: agentic-postgres\n"
            b"Requires-Dist: pydantic-ai==2.55.0; extra == 'agentic'\n"
            b"Requires-Dist: pydantic-ai==2.55.0; extra == 'agentic-postgres'\n"
            b"Requires-Dist: asyncpg>=0.30.0; extra == 'agentic-postgres'\n"
        )
    }
    manifest = {
        "extras": {
            "agentic": ["pydantic-ai==2.55.0"],
            "agentic-postgres": ["pyfly[agentic]", "asyncpg>=0.30.0"],
        }
    }
    assert checker.check_dependency_metadata(content, manifest) == 2


@pytest.mark.parametrize(
    "names",
    [
        [],
        ["candidate.whl"],
        ["candidate.tar.gz"],
        ["candidate.whl", "other.whl", "candidate.tar.gz"],
        ["candidate.whl", "candidate.tar.gz", "other.tar.gz"],
    ],
)
def test_complete_directory_requires_exactly_one_wheel_and_sdist(
    checker: Any, tmp_path: Path, names: list[str]
) -> None:
    for name in names:
        (tmp_path / name).touch()
    with pytest.raises(ValueError, match="exactly one wheel and one sdist"):
        checker.select_artifacts(tmp_path, None)


def test_complete_directory_selects_both_formats(checker: Any, tmp_path: Path) -> None:
    for name in ("candidate.whl", "candidate.tar.gz"):
        (tmp_path / name).touch()
    assert len(checker.select_artifacts(tmp_path, None)) == 2


def test_explicit_single_artifact_is_supported(checker: Any, tmp_path: Path) -> None:
    path = tmp_path / "candidate.whl"
    path.touch()
    assert checker.select_artifacts(None, [path]) == [path]


@pytest.mark.parametrize("archived", [None, b"stale"])
def test_native_data_and_admin_resources_are_required_and_current(
    checker: Any, source: Path, archived: bytes | None
) -> None:
    resource = source / "src/pyfly/admin/static/assets/favicon.svg"
    resource.parent.mkdir(parents=True)
    resource.write_bytes(b"canonical")
    files = {"pyfly/agentic/__init__.py": b"value = 1\n", "pyfly/agentic/py.typed": b""}
    if archived is not None:
        files["pyfly/admin/static/assets/favicon.svg"] = archived
    with pytest.raises(ValueError, match="Missing required archive files|Archived runtime differs"):
        checker.inspect_archive(wheel(source, files), source, {"files": []})


def test_native_extra_composes_reviewed_framework_data_dependencies(checker: Any) -> None:
    content = {
        "pyfly-1.dist-info/METADATA": (
            b"Metadata-Version: 2.4\nName: pyfly\nProvides-Extra: agentic-relational\n"
            b"Requires-Dist: sqlalchemy[asyncio]>=2.0.50; extra == 'agentic-relational'\n"
        )
    }
    manifest = {
        "extras": {"agentic-relational": ["pyfly[data-relational]"]},
        "supporting_extras": {"data-relational": ["sqlalchemy[asyncio]>=2.0.50"]},
    }
    assert checker.check_dependency_metadata(content, manifest) == 1
