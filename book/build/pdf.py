"""Render the assembled book HTML to a PDF via WeasyPrint."""
from __future__ import annotations

import base64
import re
import xml.etree.ElementTree as ET
from functools import lru_cache
from pathlib import Path

from weasyprint import CSS, HTML

_LOGO = re.compile(r"<!-- framework-logo:start -->(.*?)<!-- framework-logo:end -->", re.S)


@lru_cache(maxsize=64)
def _print_logo(source: str) -> str:
    """Keep the approved gradient/clip artwork exact without rasterizing diagram text."""
    import cairosvg

    root = ET.fromstring(source)
    box = {name: root.get(name) for name in ("x", "y", "width", "height")}
    root.attrib.pop("x", None)
    root.attrib.pop("y", None)
    width = 800
    height = round(width * float(box["height"]) / float(box["width"]))
    png = cairosvg.svg2png(bytestring=ET.tostring(root), output_width=width, output_height=height)
    uri = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    attributes = " ".join(f'{name}="{value}"' for name, value in box.items())
    return f'<image {attributes} href="{uri}" aria-hidden="true"/>'


def render_pdf(full_html: str, base_url: Path, css_paths: list[Path], out: Path) -> Path:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    full_html = _LOGO.sub(lambda match: _print_logo(match[1]), full_html)
    HTML(string=full_html, base_url=str(base_url)).write_pdf(
        str(out), stylesheets=[CSS(filename=str(path)) for path in css_paths])
    return out
