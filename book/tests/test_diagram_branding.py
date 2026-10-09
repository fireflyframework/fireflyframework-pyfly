"""Preserve diagram semantics and geometry while applying the shared identity."""
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SVG = "{http://www.w3.org/2000/svg}"
ET.register_namespace("", "http://www.w3.org/2000/svg")


def diagrams():
    if (ROOT / "assets/tools/build_brand_assets.py").exists():
        public = [ROOT / "assets" / (name + ".svg") for name in
                  ("architecture", "auto-configuration", "distributed-patterns",
                   "ecosystem", "hexagonal", "request-lifecycle")]
        return sorted(public + list((ROOT / "book/art/figures").glob("*.svg"))
                      + list((ROOT / "book/art/openers").glob("*.svg"))
                      + list((ROOT / "book/art").glob("webapps-*.svg")))
    return sorted((ROOT / "docs/assets/diagrams").glob("*.svg"))


@pytest.mark.parametrize("path", diagrams(), ids=lambda p: str(p.relative_to(ROOT)))
def test_diagram_has_self_contained_brand_footer(path):
    root = ET.fromstring(path.read_text())
    footer = root.find(f"{SVG}g[@data-framework-brand='2026-10-08']")
    assert footer is not None, f"missing approved family identity: {path}"
    assert footer.get("aria-hidden") == "true"
    assert footer.find(f".//{SVG}path") is not None
    assert not list(root.iter(f"{SVG}image")), "retired raster logo or external resource"
    assert all(not node.get("href", "").startswith(("http", "file:")) for node in root.iter())
    assert not footer.findall(f".//{SVG}text"), "family typography must stay outlined"


def test_book_interior_uses_the_shared_identity():
    css = (ROOT / "book/theme/tokens.css").read_text()
    assert "--ink:#10110f" in css
    assert "--amber:#ffb34a" in css
    assert "--page:#fffef9" not in css
    body = (ROOT / "book/theme/book.css").read_text()
    assert "#15351a" not in body
    assert "#eef3e8" not in body


def test_branding_preserves_every_original_label_and_coordinate():
    import importlib.util
    spec = importlib.util.spec_from_file_location("brand_diagrams", ROOT / "book/build/brand_diagrams.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config = json.loads((ROOT / "book/art/diagram-branding.json").read_text())
    assert set(config["diagrams"]) == {str(path.relative_to(ROOT)) for path in diagrams()}
    canonical = ET.parse(ROOT / config["logo"]).getroot()
    paths = [node.get("d") for node in canonical.iter(SVG + "path")]
    for relative, record in config["diagrams"].items():
        raw = (ROOT / relative).read_text()
        assert module.structure(raw) == record["structure"], relative
        assert module.brand(raw, relative, config) == raw, relative
        root = ET.fromstring(raw)
        old_width, old_height = map(float, record["originalViewBox"].split()[2:])
        width, height = map(float, root.get("viewBox").split()[2:])
        assert width == old_width
        assert 0 < height - old_height <= width * .08 + .01
        footer = root.find(f"{SVG}g[@data-framework-brand='2026-10-08']")
        actual = [node.get("d") for node in footer.iter(SVG + "path")]
        assert all(path in actual for path in paths), relative
        for mirror in record.get("mirrors", []):
            assert (ROOT / mirror).read_text() == raw


def test_brand_footer_does_not_shrink_long_diagram_text_in_print(tmp_path):
    import sys

    import pdfplumber
    sys.path.insert(0, str(ROOT / "book/build"))
    from pdf import render_pdf
    path = max(diagrams(), key=lambda p: float(ET.parse(p).getroot().get("viewBox").split()[3]))
    branded = path.read_text()
    root = ET.fromstring(branded)
    root.remove(root.find(f"{SVG}g[@data-framework-brand='2026-10-08']"))
    original = root.get("data-original-view-box")
    root.set("viewBox", original)
    if root.get("height"):
        root.set("height", original.split()[3])
    unbranded = ET.tostring(root, encoding="unicode")
    measurements = []
    for name, source in [("branded", branded), ("without-footer", unbranded)]:
        output = tmp_path / (name + ".pdf")
        render_pdf('<html><body><figure class="fig">' + source + '</figure></body></html>',
                   ROOT / "book", [ROOT / "book/theme" / file for file in
                   ("tokens.css", "book.css", "print.css")], output)
        with pdfplumber.open(output) as pdf:
            assert len(pdf.pages) == 1, "long diagram no longer fits the print text area"
            page = pdf.pages[0]
            chars = [char for char in page.chars if char["text"].strip()]
            assert all(0 <= char["x0"] < char["x1"] <= page.width for char in chars)
            assert all(0 <= char["top"] < char["bottom"] <= page.height for char in chars)
            measurements.append([(char["text"], round(char["size"], 2), round(char["x0"], 2))
                                 for char in chars])
    assert measurements[0] == measurements[1], "footer changed original text size or horizontal position"


def test_pdf_keeps_diagram_text_vector_and_rasterizes_only_the_canonical_logo(tmp_path):
    import sys

    import pdfplumber
    sys.path.insert(0, str(ROOT / "book/build"))
    from pdf import render_pdf
    path = diagrams()[0]
    output = tmp_path / "diagram-logo.pdf"
    render_pdf('<html><body><figure class="fig">' + path.read_text() + '</figure></body></html>',
               ROOT / "book", [ROOT / "book/theme" / file for file in
               ("tokens.css", "book.css", "print.css")], output)
    with pdfplumber.open(output) as pdf:
        assert len(pdf.pages) == 1
        page = pdf.pages[0]
        assert len(page.chars) > 100, "diagram labels must remain selectable vector text"
        assert len(page.images) == 1, "only the approved footer lockup needs raster fidelity"
        logo = page.images[0]
        assert logo["width"] < page.width * .2
        assert logo["height"] < page.height * .1


def test_constant_ink_gradient_headers_remain_visible_in_pdf(tmp_path):
    import sys

    import pdfplumber
    sys.path.insert(0, str(ROOT / "book/build"))
    from pdf import render_pdf
    source = (ROOT / "book/art/figures/01-choice.svg").read_text()
    output = tmp_path / "diagram-headers.pdf"
    render_pdf('<html><body><figure class="fig">' + source + '</figure></body></html>',
               ROOT / "book", [ROOT / "book/theme" / file for file in
               ("tokens.css", "book.css", "print.css")], output)
    with pdfplumber.open(output) as pdf:
        page = pdf.pages[0]
        opaque_ink_bands = [shape for shape in page.curves + page.rects if shape.get("fill")
                           and shape["width"] > 100 and isinstance(shape.get("non_stroking_color"), (tuple, list))
                           and max(shape["non_stroking_color"]) < .1]
        assert len(opaque_ink_bands) >= 2, "white header labels lost their solid ink background"
