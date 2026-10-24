"""The dashboard ships the canonical outlined pyfly identity and local font."""

import hashlib
import json
from importlib.resources import files


def test_brand_resources_match_pinned_canonical_provenance():
    assets = files("pyfly.admin") / "static" / "assets"
    manifest = json.loads((assets / "brand-origin.json").read_text())
    assert manifest["repository"] == "https://github.com/fireflyframework/brand-kit"
    assert manifest["commit"] == "b64a50e289b853cab54e5d59bc1b092b48c5eec4"
    for name, entry in manifest["files"].items():
        assert hashlib.sha256((assets / name).read_bytes()).hexdigest() == entry["sha256"]
    for theme in ("light", "dark"):
        logo = (assets / f"pyfly-{theme}.svg").read_text()
        assert 'data-family-terminal="trail-y"' in logo
        assert "#ffb34a" in logo
    assert not (assets / "pyfly-logo.png").is_file()
    css = (files("pyfly.admin") / "static" / "css" / "admin.css").read_text()
    assert "fonts.googleapis" not in css
    assert "Manrope-variable.woff2" in css


def test_dashboard_links_the_packaged_canonical_favicon():
    markup = (files("pyfly.admin") / "static" / "index.html").read_text()
    assert 'href="static/assets/favicon.svg"' in markup
    assert (files("pyfly.admin") / "static/assets/favicon.svg").is_file()
