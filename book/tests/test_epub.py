# tests/book/test_epub.py
import zipfile
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "book" / "build"))
from epub import EpubBuilder, Doc  # noqa: E402

def test_epub_is_valid_ocf(tmp_path):
    out = tmp_path / "b.epub"
    b = EpubBuilder(title="T", author="A", language="en", identifier="urn:uuid:1",
                    css=["body{color:#000}"])
    b.add_doc(Doc(id="c1", title="One", xhtml_body="<h1>One</h1>", in_nav=True))
    b.build(out)
    z = zipfile.ZipFile(out)
    # mimetype must be first entry, stored (uncompressed), exact bytes
    first = z.infolist()[0]
    assert first.filename == "mimetype"
    assert first.compress_type == zipfile.ZIP_STORED
    assert z.read("mimetype") == b"application/epub+zip"
    names = set(z.namelist())
    assert "META-INF/container.xml" in names
    assert "OEBPS/content.opf" in names
    assert "OEBPS/nav.xhtml" in names
    assert "OEBPS/c1.xhtml" in names
    assert "One" in z.read("OEBPS/nav.xhtml").decode()
