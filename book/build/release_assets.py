"""Create and verify the four book release assets and their source manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree

BOOK_FILES = (
    "pyfly-by-example.pdf",
    "pyfly-by-example.epub",
    "pyfly-by-example-es.pdf",
    "pyfly-by-example-es.epub",
)
MANIFEST = "books.sha256.json"


def _validate_book(path: Path) -> None:
    if path.suffix == ".pdf":
        with path.open("rb") as stream:
            if stream.read(5) != b"%PDF-":
                raise ValueError(f"Invalid PDF header: {path.name}")
            stream.seek(-min(path.stat().st_size, 1024), 2)
            if b"%%EOF" not in stream.read():
                raise ValueError(f"Missing PDF trailer: {path.name}")
        return

    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if not entries or entries[0].filename != "mimetype" or entries[0].compress_type != zipfile.ZIP_STORED:
            raise ValueError(f"Invalid EPUB mimetype entry: {path.name}")
        if archive.read("mimetype") != b"application/epub+zip":
            raise ValueError(f"Invalid EPUB mimetype: {path.name}")
        if archive.testzip() is not None:
            raise ValueError(f"Corrupt EPUB: {path.name}")
        for required in ("META-INF/container.xml", "OEBPS/content.opf", "OEBPS/nav.xhtml"):
            if required not in archive.namelist():
                raise ValueError(f"Missing EPUB entry {required}: {path.name}")
        opf = ElementTree.fromstring(archive.read("OEBPS/content.opf"))
        nav = ElementTree.fromstring(archive.read("OEBPS/nav.xhtml"))
        if not opf.findall(".//{http://www.idpf.org/2007/opf}itemref"):
            raise ValueError(f"Empty EPUB spine: {path.name}")
        links = nav.findall(".//{http://www.w3.org/1999/xhtml}a")
        if not links or any(f"OEBPS/{link.get('href')}" not in archive.namelist() for link in links):
            raise ValueError(f"Invalid EPUB navigation: {path.name}")


def _hashes(directory: Path) -> dict[str, str]:
    actual = {p.name for p in directory.iterdir() if p.is_file()}
    expected = set(BOOK_FILES)
    if actual - {MANIFEST} != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected - {MANIFEST})
        raise ValueError(f"Book assets differ: missing={missing}, extra={extra}")
    hashes = {}
    for name in BOOK_FILES:
        path = directory / name
        _validate_book(path)
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("create", "verify"))
    parser.add_argument("--dir", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args(argv)
    try:
        if not re.fullmatch(r"[0-9a-f]{40}", args.commit):
            raise ValueError("Expected a 40-character source commit SHA")
        hashes = _hashes(args.dir)
        manifest_path = args.dir / MANIFEST
        if args.action == "create":
            if manifest_path.exists():
                raise ValueError("Manifest already exists; release output must be fresh")
            manifest_path.write_text(json.dumps({"source_commit": args.commit, "sha256": hashes}, indent=2) + "\n")
        else:
            manifest = json.loads(manifest_path.read_text())
            if manifest != {"source_commit": args.commit, "sha256": hashes}:
                raise ValueError("Book manifest commit or checksums do not match release assets")
        print(f"Verified {len(hashes)} book assets for {args.commit}")
        return 0
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
        print(f"Book release validation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
