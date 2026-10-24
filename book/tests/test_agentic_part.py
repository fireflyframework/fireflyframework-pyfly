"""The final native part stays bilingual and tied to executable listings."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
BOOK = ROOT / "book"


def test_both_editions_have_matching_final_agentic_part():
    editions = [yaml.safe_load((BOOK / name).read_text()) for name in ("book.yaml", "book.es.yaml")]
    for edition in editions:
        part = edition["parts"][-2]
        assert "VI" in part["title"]
        assert [chapter["id"] for chapter in part["chapters"]] == ["ch19", "ch20", "ch21"]
        for chapter in part["chapters"]:
            text = (BOOK / edition["manuscript_dir"] / chapter["file"]).read_text()
            assert len(text.split()) >= 850
            assert "::: figure art/figures/" in text
            assert "pyfly.agentic" in text


def test_agentic_listings_are_verbatim_companion_fragments():
    for edition in ("manuscript", "manuscript-es"):
        for chapter in sorted((BOOK / edition).glob("[12][901]-agentic-*.md")):
            text = chapter.read_text()
            listings = re.findall(r"::: listing (samples/agentic-book/[^ |]+\.py) [^\n]*\n(.*?)\n:::", text, re.S)
            assert listings, chapter
            for path, code in listings:
                assert code.strip() in (ROOT / path).read_text(), (chapter, path)
