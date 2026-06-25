#!/usr/bin/env python3
"""Assemble the full PyFly GitHub Pages site into ``_site/``.

Layout produced (served at https://fireflyframework.github.io/fireflyframework-pyfly/):

    _site/
    ├── index.html, styles.css, app.js   # the landing page (from web/)
    ├── assets/                           # brand SVGs + logo (from assets/)
    ├── .nojekyll                         # serve _-prefixed paths verbatim
    └── docs/                             # MkDocs Material build (from docs/ via mkdocs.yml)

This is the single source of truth used both locally and by the GitHub Actions
workflow, so what CI deploys is exactly what you can preview on your machine:

    python scripts/build_site.py
    python -m http.server -d _site 8000   # then open http://localhost:8000/
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
ASSETS = ROOT / "assets"
OUT = ROOT / "_site"

LANDING_FILES = ("index.html", "styles.css", "app.js")
# Brand assets the landing page references (assets/<name>); copied to _site/assets/.
ASSET_GLOBS = ("*.svg", "pyfly-logo.png")


def log(msg: str) -> None:
    print(f"[build-site] {msg}")


def clean() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    log(f"cleaned {OUT.relative_to(ROOT)}/")


def build_docs() -> None:
    """Render docs/ into _site/docs via MkDocs Material."""
    target = OUT / "docs"
    log("building docs with MkDocs Material …")
    try:
        subprocess.run(
            [sys.executable, "-m", "mkdocs", "build", "--clean", "-d", str(target)],
            cwd=ROOT,
            check=True,
        )
    except FileNotFoundError:
        sys.exit("error: mkdocs not found. Install with: pip install -r requirements-docs.txt")
    except subprocess.CalledProcessError as exc:
        sys.exit(f"error: mkdocs build failed (exit {exc.returncode}).")
    log(f"docs → {target.relative_to(ROOT)}/")


def copy_landing() -> None:
    missing = [f for f in LANDING_FILES if not (WEB / f).exists()]
    if missing:
        sys.exit(f"error: missing landing files in web/: {', '.join(missing)}")
    for name in LANDING_FILES:
        shutil.copy2(WEB / name, OUT / name)
    log(f"landing → {', '.join(LANDING_FILES)}")


def copy_assets() -> None:
    dest = OUT / "assets"
    dest.mkdir(parents=True, exist_ok=True)
    copied = 0
    for pattern in ASSET_GLOBS:
        for src in sorted(ASSETS.glob(pattern)):
            shutil.copy2(src, dest / src.name)
            copied += 1
    if copied == 0:
        sys.exit(f"error: no brand assets matched {ASSET_GLOBS} in {ASSETS}")
    log(f"assets → {copied} file(s) into assets/")


def finalize() -> None:
    # .nojekyll keeps GitHub Pages from running Jekyll, which would strip
    # any path beginning with an underscore.
    (OUT / ".nojekyll").write_text("", encoding="utf-8")
    log("wrote .nojekyll")


def main() -> None:
    clean()
    build_docs()
    copy_landing()
    copy_assets()
    finalize()
    log(f"done → open {OUT.relative_to(ROOT)}/index.html")


if __name__ == "__main__":
    main()
