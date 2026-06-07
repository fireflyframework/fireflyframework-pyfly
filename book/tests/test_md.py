# tests/book/test_md.py
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "book" / "build"))
from md import render_markdown  # noqa: E402

def test_listing_renders_filetab_caption_and_highlight(tmp_path):
    src = (
        "::: listing wallet/app.py | Listing 1.1 — hello\n"
        "from pyfly.core import pyfly_application\n"
        "@pyfly_application(name=\"x\")\n"
        "class App: ...\n"
        ":::\n"
    )
    html = render_markdown(src, tmp_path)
    assert 'class="filetab">wallet/app.py<' in html
    assert "Listing 1.1" in html
    assert 'class="listing"' in html
    assert "<span" in html  # pygments emitted token spans

def test_figure_inlines_svg(tmp_path):
    (tmp_path / "f.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>')
    html = render_markdown("::: figure f.svg | Figure 1.1 — demo\n", tmp_path)
    assert "<figure" in html and "<svg" in html and "Figure 1.1" in html

def test_spring_callout(tmp_path):
    html = render_markdown('!!! spring "Spring parity"\n    Same as Spring.\n', tmp_path)
    assert "admonition spring" in html and "Spring parity" in html
