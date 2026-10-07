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
    assert f"| `{__version__}` |" in versioning
    assert f"pyfly --version            # → {__version__}" in versioning
    assert f":: PyFly Framework :: ({display}) (Python" in versioning
    assert f":: PyFly :: ({display})" in (root / "docs/modules/core.md").read_text(encoding="utf-8")
