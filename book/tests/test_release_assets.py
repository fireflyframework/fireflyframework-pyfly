"""Checks for isolated book builds and release assets."""

import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "build"))
import build as book_build  # noqa: E402
from epub import Doc, EpubBuilder  # noqa: E402

BOOK = Path(__file__).resolve().parents[1]
RELEASE_ASSETS = BOOK / "build" / "release_assets.py"
NAMES = (
    "pyfly-by-example.pdf",
    "pyfly-by-example.epub",
    "pyfly-by-example-es.pdf",
    "pyfly-by-example-es.epub",
)
COMMIT = "a" * 40


def _assets(directory: Path) -> None:
    directory.mkdir()
    for name in NAMES:
        path = directory / name
        if name.endswith(".pdf"):
            path.write_bytes(b"%PDF-1.7\n1 0 obj\n%%EOF\n")
        else:
            epub = EpubBuilder(
                title=name,
                author="A",
                language="es" if "-es" in name else "en",
                identifier=f"urn:uuid:{name}",
                css=["body{}"],
            )
            epub.add_doc(Doc(id="chapter", title="Chapter", xhtml_body="<h1>Chapter</h1>", in_nav=True))
            epub.build(path)


def _release_command(action: str, directory: Path, commit: str = COMMIT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(RELEASE_ASSETS), action, "--dir", str(directory), "--commit", commit],
        capture_output=True,
        text=True,
        check=False,
    )


def test_build_can_write_outside_tracked_dist(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    manuscript = source / "manuscript"
    manuscript.mkdir()
    (manuscript / "chapter.md").write_text("# Hello\n")
    (source / "book.yaml").write_text(
        "title: Book\nauthor: Author\nlanguage: en\nidentifier: urn:uuid:book\n"
        "manuscript_dir: manuscript\ncover_png: missing.png\noutput_basename: fresh\n"
        "parts:\n  - title: Part I\n    chapters:\n"
        "      - {id: chapter, file: chapter.md, num: 1, title: Hello}\n"
    )
    tracked = tmp_path / "tracked"
    tracked.mkdir()
    (tracked / "old.pdf").write_bytes(b"owner work")
    out = tmp_path / "release"
    monkeypatch.setattr(book_build, "BOOK", source)
    monkeypatch.setattr(book_build, "DIST", tracked)
    monkeypatch.setattr(book_build, "render_pdf", lambda *args, **kwargs: kwargs["out"].write_bytes(b"%PDF-1.7\n%%EOF"))

    assert book_build.main(["--out-dir", str(out)]) == 0
    assert (out / "fresh.epub").exists()
    assert (out / "fresh.pdf").read_bytes().startswith(b"%PDF-")
    assert sorted(p.name for p in tracked.iterdir()) == ["old.pdf"]
    assert (tracked / "old.pdf").read_bytes() == b"owner work"


@pytest.mark.parametrize("missing_file", ["front.md", "chapter.md"])
def test_build_rejects_missing_configured_manuscript_before_writing(tmp_path, monkeypatch, missing_file):
    source = tmp_path / "source"
    manuscript = source / "manuscript"
    manuscript.mkdir(parents=True)
    for name in ("front.md", "chapter.md"):
        if name != missing_file:
            (manuscript / name).write_text("# Present\n")
    (source / "book.yaml").write_text(
        "title: Book\nauthor: Author\nlanguage: en\nidentifier: urn:uuid:book\n"
        "manuscript_dir: manuscript\ncover_png: missing.png\noutput_basename: fresh\n"
        "front:\n  - {id: front, file: front.md, title: Front}\n"
        "parts:\n  - title: Part I\n    chapters:\n"
        "      - {id: chapter, file: chapter.md, num: 1, title: Hello}\n"
    )
    out = tmp_path / "release"
    monkeypatch.setattr(book_build, "BOOK", source)

    with pytest.raises(FileNotFoundError, match=missing_file):
        book_build.main(["--out-dir", str(out)])
    assert not out.exists()


def test_manifest_records_exact_bytes_and_commit(tmp_path):
    out = tmp_path / "release"
    _assets(out)
    result = _release_command("create", out)
    assert result.returncode == 0, result.stderr
    manifest = json.loads((out / "books.sha256.json").read_text())
    assert manifest["source_commit"] == COMMIT
    assert manifest["sha256"] == {name: hashlib.sha256((out / name).read_bytes()).hexdigest() for name in NAMES}
    assert _release_command("verify", out).returncode == 0


def test_missing_and_extra_assets_fail_release(tmp_path):
    out = tmp_path / "release"
    _assets(out)
    (out / NAMES[0]).unlink()
    assert _release_command("create", out).returncode != 0
    (out / NAMES[0]).write_bytes(b"%PDF-1.7\n%%EOF\n")
    assert _release_command("create", out).returncode == 0
    (out / "stale.pdf").write_bytes(b"%PDF-1.7\n%%EOF\n")
    assert _release_command("verify", out).returncode != 0


def test_changed_bytes_and_wrong_commit_fail_release(tmp_path):
    out = tmp_path / "release"
    _assets(out)
    assert _release_command("create", out).returncode == 0
    assert _release_command("verify", out, "b" * 40).returncode != 0
    (out / NAMES[1]).write_bytes((out / NAMES[1]).read_bytes() + b"changed")
    assert _release_command("verify", out).returncode != 0


def test_invalid_pdf_and_epub_fail_before_manifest(tmp_path):
    out = tmp_path / "release"
    _assets(out)
    (out / NAMES[0]).write_bytes(b"not a PDF")
    assert _release_command("create", out).returncode != 0
    (out / NAMES[0]).write_bytes(b"%PDF-1.7\n%%EOF\n")
    with zipfile.ZipFile(out / NAMES[1], "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
    assert _release_command("create", out).returncode != 0
