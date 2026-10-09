"""Covers survive publication as accessible bookends, without print margins."""

import shutil
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pdfplumber
import pytest
import yaml
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "build"))
import build as book_build  # noqa: E402
import gen_cover  # noqa: E402

BOOK = Path(__file__).resolve().parents[1]
NS = {"opf": "http://www.idpf.org/2007/opf", "html": "http://www.w3.org/1999/xhtml"}


@pytest.fixture
def edition(tmp_path, monkeypatch):
    source = tmp_path / "book"
    source.mkdir()
    (source / "art").mkdir()
    (source / "manuscript").mkdir()
    (source / "manuscript" / "chapter.md").write_text("# A short chapter\n\nThe business application.\n")
    for name, color in (("cover-es.png", "#112233"), ("back-cover-es.png", "#445566")):
        Image.new("RGB", (1500, 1850), color).save(source / "art" / name)
    cfg = {
        "title": "Libro & ejemplo", "subtitle": "Sistemas & negocio", "author": "Firefly", "language": "es",
        "identifier": "urn:uuid:cover-test", "output_basename": "edition",
        "manuscript_dir": "manuscript", "cover_png": "art/cover-es.png",
        "cover_alt": 'Portada de "Libro & ejemplo"', "back_cover_png": "art/back-cover-es.png",
        "back_cover_alt": "Contraportada: arquitectura & aplicaciones",
        "labels": {"contents": "Contenido", "cover": "Portada", "back_cover": "Contraportada"},
        "parts": [{"title": "Parte I — Aplicaciones", "chapters": [
            {"id": "ch01", "file": "chapter.md", "num": 1, "title": "Aplicaciones"}
        ]}],
    }
    (source / "book.yaml").write_text(yaml.safe_dump(cfg, allow_unicode=True))
    out = tmp_path / "output"
    monkeypatch.setattr(book_build, "BOOK", source)
    monkeypatch.setattr(book_build, "THEME", BOOK / "theme")
    monkeypatch.setattr(book_build, "DIST", out)
    return source, out, cfg


def test_epub_has_localized_cover_bookends_and_one_cover_image(edition):
    source, out, cfg = edition
    assert book_build.main([]) == 0
    with zipfile.ZipFile(out / "edition.epub") as archive:
        opf = ET.fromstring(archive.read("OEBPS/content.opf"))
        spine = [item.get("idref") for item in opf.findall("opf:spine/opf:itemref", NS)]
        assert spine[0] == "cover"
        assert spine[-1] == "back-cover"
        covers = opf.findall('opf:manifest/opf:item[@properties="cover-image"]', NS)
        assert len(covers) == 1
        assert archive.read("OEBPS/" + covers[0].get("href")) == (source / cfg["cover_png"]).read_bytes()
        for doc, alt in (("cover", cfg["cover_alt"]), ("back-cover", cfg["back_cover_alt"])):
            xhtml = ET.fromstring(archive.read(f"OEBPS/{doc}.xhtml"))
            img = xhtml.find(".//html:img", NS)
            assert img is not None
            assert img.get("alt") == alt
            assert "OEBPS/" + img.get("src") in archive.namelist()
        nav = ET.fromstring(archive.read("OEBPS/nav.xhtml"))
        links = [(a.get("href"), a.text) for a in nav.findall(".//html:a", NS)]
        assert links[0] == ("cover.xhtml", "Portada")
        assert links[-1] == ("back-cover.xhtml", "Contraportada")


def test_pdf_covers_fill_first_and_last_page_without_headers(edition):
    _, out, _ = edition
    assert book_build.main([]) == 0
    with pdfplumber.open(out / "edition.pdf") as pdf:
        assert len(pdf.pages) == 5  # front, contents, part divider, chapter, back
        for page in (pdf.pages[0], pdf.pages[-1]):
            assert (page.width, page.height) == pytest.approx((540, 666))
            assert not page.chars
            assert len(page.images) == 1
            img = page.images[0]
            assert (img["x0"], img["top"], img["x1"], img["bottom"]) == pytest.approx((0, 0, 540, 666))
        assert "business application" in (pdf.pages[-2].extract_text() or "")


@pytest.mark.parametrize("key", ["cover_png", "back_cover_png"])
def test_missing_configured_cover_fails_before_publishing(edition, key):
    source, out, cfg = edition
    (source / cfg[key]).unlink()
    with pytest.raises(FileNotFoundError, match=Path(cfg[key]).name):
        book_build.main([])
    assert not out.exists()


@pytest.fixture
def canonical_art(tmp_path, monkeypatch):
    art = tmp_path / "art"
    art.mkdir()
    for name in ("cover", "cover-es", "back-cover", "back-cover-es"):
        (art / f"{name}.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="1500" height="1850" viewBox="0 0 1500 1850">'
            '<path fill="#123456" d="M0 0h1500v1850H0z"/></svg>'
        )
        Image.new("RGB", (1500, 1850), "#123456").save(art / f"{name}.png")
    # The legacy PyFly generator loads its logo before overwriting the cover.
    (art / "logo").mkdir()
    shutil.copyfile(art / "cover.png", art / "logo" / "pyfly-logo.png")
    monkeypatch.setattr(gen_cover, "ART", art)
    monkeypatch.setattr(sys, "argv", ["gen_cover.py"])
    return art


def test_cover_command_preserves_canonical_art(canonical_art):
    before = {p.name: p.read_bytes() for p in canonical_art.iterdir() if p.is_file()}
    gen_cover.main()
    assert {p.name: p.read_bytes() for p in canonical_art.iterdir() if p.is_file()} == before


@pytest.mark.parametrize("invalid", ["geometry", "text"])
def test_cover_command_rejects_noncanonical_svg(canonical_art, invalid):
    svg = canonical_art / "cover.svg"
    original = svg.read_text()
    bad = original.replace("1500 1850", "1500 2100") if invalid == "geometry" else original.replace("</svg>", "<text>Unoutlined</text></svg>")
    svg.write_text(bad)
    with pytest.raises(ValueError):
        gen_cover.main()
    assert svg.read_text() == bad


def test_epub_navigation_uses_the_edition_contents_label():
    epub = book_build.EpubBuilder(title="Libro", author="Firefly", language="es", identifier="test")
    epub.add_doc(book_build.Doc(id="toc", title="Contenido", xhtml_body="<h1>Contenido</h1>", kind="toc"))
    nav = ET.fromstring(epub._nav())
    assert nav.find(".//html:h1", NS).text == "Contenido"
    assert nav.get("lang") == "es"


def test_explicit_render_rebuilds_png_from_canonical_svg(canonical_art):
    target = canonical_art / "cover-es.png"
    Image.new("RGB", (1500, 1850), "#ffffff").save(target)
    source = (canonical_art / "cover-es.svg").read_bytes()
    gen_cover.main(["--render"])
    with Image.open(target) as image:
        assert image.getpixel((750, 925))[:3] == (18, 52, 86)
    assert (canonical_art / "cover-es.svg").read_bytes() == source


def test_pdf_metadata_uses_the_edition_title(edition):
    _, out, cfg = edition
    assert book_build.main([]) == 0
    with pdfplumber.open(out / "edition.pdf") as pdf:
        assert pdf.metadata.get("Title") == cfg["title"]
        assert pdf.metadata.get("Author") == cfg["author"]
        assert pdf.metadata.get("Subject") == cfg["subtitle"]


def test_title_page_does_not_repeat_an_uppercased_book_name(edition):
    source, out, cfg = edition
    (source / "manuscript" / "title.md").write_text("# pyfly by example {.chtitle}\n")
    cfg["front"] = [{"id": "title", "file": "title.md", "nav": False}]
    (source / "book.yaml").write_text(yaml.safe_dump(cfg, allow_unicode=True))
    assert book_build.main([]) == 0
    with pdfplumber.open(out / "edition.pdf") as pdf:
        title_text = pdf.pages[1].extract_text()
        assert title_text.count("pyfly by example") == 1
        assert "PYFLY BY EXAMPLE" not in title_text
