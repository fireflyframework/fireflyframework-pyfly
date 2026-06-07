"""Render the assembled book HTML to a PDF via WeasyPrint."""
from __future__ import annotations
from pathlib import Path
from weasyprint import HTML, CSS

def render_pdf(full_html: str, base_url: Path, css_paths: list[Path], out: Path) -> Path:
    out = Path(out); out.parent.mkdir(parents=True, exist_ok=True)
    HTML(string=full_html, base_url=str(base_url)).write_pdf(
        str(out), stylesheets=[CSS(filename=str(p)) for p in css_paths])
    return out
