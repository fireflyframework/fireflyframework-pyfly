# Copyright 2026 Firefly Software Foundation.
# SPDX-License-Identifier: Apache-2.0
"""Prevent active package versions and published download examples from drifting."""

import ast
import importlib.metadata
import os
import textwrap
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from pyfly import __version__


def test_release_version_matches_metadata_badges_and_downloads():
    root = Path(__file__).resolve().parents[2]
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    assert version == ".".join(str(int(part)) for part in __version__.split("."))
    readme = (root / "README.md").read_text()
    assert f"version-{__version__}-brightgreen" in readme
    assert f"pyfly-{version}-py3-none-any.whl" in readme
    assert f">v{__version__}</span>" in (root / "web/index.html").read_text()
    assert f"## v{__version__} " in (root / "CHANGELOG.md").read_text()


def test_every_displayed_version_site_shows_the_release() -> None:
    root = Path(__file__).resolve().parents[2]
    display = f"v{__version__}"
    sites = {
        "install.sh": f'PYFLY_VERSION="{__version__}"',
        "README.md": f"- **`{display}`** (",
        "docs/versioning.md": f'print(pyfly.__version__)  # → "{__version__}"',
        "docs/getting-started.md": f"PyFly {display} | Python",
        "docs/installation.md": f"✓ pyfly {display}",
        "docs/cli.md": f"✓ pyfly {display}",
        "docs/modules/core.md": f":: PyFly Framework :: ({display})",
    }
    for path, expected in sites.items():
        assert expected in (root / path).read_text(encoding="utf-8"), path
    versioning = (root / "docs/versioning.md").read_text(encoding="utf-8")
    package_version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    assert f"| `{__version__}` |" in versioning
    assert f"pyfly --version            # → pyfly, version {package_version}" in versioning
    assert f":: PyFly Framework :: ({display}) (Python" in versioning
    assert f":: PyFly :: ({display})" in (root / "docs/modules/core.md").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("display", "metadata", "valid"),
    [
        ("26.11.01", "26.11.1", True),
        ("26.10.32", "26.11.1", False),
        ("26.11.01", "26.10.32", False),
        ("26.10.32", "26.10.32", False),
        ("26.11.01", "26.11.01", False),
    ],
)
def test_persistent_wheel_ci_checks_both_version_contracts(tmp_path, monkeypatch, display, metadata, valid):
    root = Path(__file__).resolve().parents[2]
    workflow = (root / ".github/workflows/ci.yml").read_text()
    step = workflow.split("- name: Require the real optional runtime and installed namespace", 1)[1]
    program = textwrap.dedent(step.split("persistent-wheel/bin/python - <<'PY'\n", 1)[1].split("\n          PY", 1)[0])
    parsed = ast.parse(program)
    prefix = []
    for statement in parsed.body:
        if isinstance(statement, ast.With):
            break
        if not isinstance(statement, (ast.Import, ast.ImportFrom)):
            prefix.append(statement)
    (tmp_path / "src/pyfly").mkdir(parents=True)
    (tmp_path / "src/pyfly/__init__.py").write_text('__version__ = "26.11.01"\n')
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "26.11.1"\n')
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(importlib.metadata, "version", lambda _: metadata)
    namespace = {
        **globals(),
        "os": os,
        "pyfly": SimpleNamespace(__version__=display, __file__="/isolated/site-packages/pyfly/__init__.py"),
    }
    code = compile(ast.Module(body=prefix, type_ignores=[]), "<persistent-wheel-ci>", "exec")
    if valid:
        exec(code, namespace)
    else:
        with pytest.raises(AssertionError):
            exec(code, namespace)
