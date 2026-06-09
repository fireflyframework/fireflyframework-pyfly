# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Detect the current PyFly project's shape for code generators."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class ProjectNotFoundError(Exception):
    """Raised when no PyFly/Python project is found from the start directory."""


@dataclass(frozen=True)
class ProjectInfo:
    """Resolved layout of the project the generator runs against."""

    root: Path
    package: str
    archetype: str
    src_dir: Path
    package_dir: Path
    tests_dir: Path


def _find_root(start: Path) -> Path:
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise ProjectNotFoundError("No pyproject.toml found. Run 'pyfly generate' inside a PyFly project.")


def _read_yaml(root: Path) -> dict[str, object]:
    path = root / "pyfly.yaml"
    if not path.is_file():
        return {}
    try:
        import yaml  # type: ignore[import-untyped]

        with open(path) as f:
            result = yaml.safe_load(f)
            if isinstance(result, dict):
                return result
            return {}
    except Exception:  # noqa: BLE001
        return {}


def _detect_package(root: Path, data: dict[str, object]) -> str:
    pyfly = data.get("pyfly")
    app: dict[str, object] = {}
    if isinstance(pyfly, dict):
        raw_app = pyfly.get("app")
        if isinstance(raw_app, dict):
            app = raw_app
    module = app.get("module")
    if isinstance(module, str) and module:
        return module.split(".")[0]
    src = root / "src"
    if src.is_dir():
        pkgs = [p.name for p in sorted(src.iterdir()) if p.is_dir() and (p / "__init__.py").exists()]
        if pkgs:
            return pkgs[0]
    raise ProjectNotFoundError("Could not determine the project package under src/.")


def _detect_archetype(root: Path, package: str, data: dict[str, object]) -> str:
    pyfly = data.get("pyfly")
    app: dict[str, object] = {}
    if isinstance(pyfly, dict):
        raw_app = pyfly.get("app")
        if isinstance(raw_app, dict):
            app = raw_app
    archetype = app.get("archetype")
    if isinstance(archetype, str) and archetype:
        return archetype
    pkg_dir = root / "src" / package
    if (pkg_dir / "domain").is_dir():
        return "hexagonal"
    if (pkg_dir / "templates").is_dir():
        return "web"
    if (pkg_dir / "controllers").is_dir():
        return "web-api"
    if (pkg_dir / "commands").is_dir():
        return "cli"
    return "core"


def detect_project(start: Path | None = None) -> ProjectInfo:
    """Resolve the current project's package, archetype, and directories."""
    root = _find_root(start or Path.cwd())
    data = _read_yaml(root)
    package = _detect_package(root, data)
    archetype = _detect_archetype(root, package, data)
    return ProjectInfo(
        root=root,
        package=package,
        archetype=archetype,
        src_dir=root / "src",
        package_dir=root / "src" / package,
        tests_dir=root / "tests",
    )


def feature_flags(info: ProjectInfo) -> dict[str, bool]:
    """Derive has_* flags from pyfly.yaml so generators match the project's stack."""
    data = _read_yaml(info.root)
    pyfly: dict[str, object] = {}
    raw_pyfly = data.get("pyfly")
    if isinstance(raw_pyfly, dict):
        pyfly = raw_pyfly

    data_section: dict[str, object] = {}
    raw_data = pyfly.get("data")
    if isinstance(raw_data, dict):
        data_section = raw_data

    relational_enabled: bool = False
    raw_relational = data_section.get("relational")
    if isinstance(raw_relational, dict):
        val = raw_relational.get("enabled")
        relational_enabled = bool(val)

    document_enabled: bool = False
    raw_document = data_section.get("document")
    if isinstance(raw_document, dict):
        val = raw_document.get("enabled")
        document_enabled = bool(val)

    fastapi_from_web: bool = False
    raw_web = pyfly.get("web")
    if isinstance(raw_web, dict):
        fastapi_from_web = raw_web.get("adapter") == "fastapi"

    return {
        "has_data": relational_enabled,
        "has_mongodb": document_enabled,
        "has_fastapi": info.archetype == "fastapi-api" or fastapi_from_web,
    }
