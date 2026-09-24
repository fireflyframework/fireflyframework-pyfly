"""Prevent active package versions and published download examples from drifting."""

import tomllib
from pathlib import Path

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
