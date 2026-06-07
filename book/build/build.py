"""Build *PyFly by Example* into EPUB + PDF from book.yaml."""
from __future__ import annotations
import sys
from pathlib import Path
import yaml  # PyYAML ships with the framework env; ensure installed in book/.venv

sys.path.insert(0, str(Path(__file__).resolve().parent))
from md import render_markdown          # noqa: E402
from epub import EpubBuilder, Doc       # noqa: E402
from pdf import render_pdf              # noqa: E402

BOOK = Path(__file__).resolve().parents[1]
MAN = BOOK / "manuscript"
THEME = BOOK / "theme"
DIST = BOOK / "dist"

def _docs_from_manifest(cfg: dict) -> list[tuple[str, str, str, bool]]:
    """Return (id, title, md_path, in_nav) in reading order; skip missing files."""
    out = []
    for fm in cfg.get("front", []):
        out.append((fm["id"], fm.get("title", fm["id"].title()),
                    str(MAN / fm["file"]), bool(fm.get("nav", True)) and "title" in fm))
    for part in cfg.get("parts", []):
        for ch in part["chapters"]:
            out.append((ch["id"], f'{ch["num"]}. {ch["title"]}', str(MAN / ch["file"]), True))
    return [(i, t, p, n) for (i, t, p, n) in out if Path(p).exists()]

def main() -> int:
    cfg = yaml.safe_load((BOOK / "book.yaml").read_text())
    css_files = [THEME / "book.css"]            # @import pulls tokens + pygments
    css_text = [(THEME / "book.css").read_text(), (THEME / "tokens.css").read_text(),
                (THEME / "pygments.css").read_text()]
    docs = _docs_from_manifest(cfg)

    # ---- EPUB ----
    epub = EpubBuilder(title=cfg["title"], author=cfg["author"], language=cfg["language"],
                       identifier=cfg["identifier"], css=css_text)
    # cover image first
    cover_png = BOOK / cfg["cover_png"]
    if cover_png.exists():
        epub.add_file(cover_png, "art/cover.png", "cover-img", properties="cover-image")
    for (cid, title, path, in_nav) in docs:
        body = render_markdown(Path(path).read_text(encoding="utf-8"), BOOK)
        epub.add_doc(Doc(id=cid, title=title, xhtml_body=body, in_nav=in_nav))
    DIST.mkdir(exist_ok=True)
    epub.build(DIST / "pyfly-by-example.epub")

    # ---- PDF ----
    parts_html = []
    if cover_png.exists():
        parts_html.append(f'<div class="cover-page"><img src="{cfg["cover_png"]}"/></div>')
    for (cid, title, path, in_nav) in docs:
        parts_html.append(render_markdown(Path(path).read_text(encoding="utf-8"), BOOK))
    full = ("<!DOCTYPE html><html><head><meta charset='utf-8'></head><body>"
            + "\n".join(parts_html) + "</body></html>")
    render_pdf(full, base_url=BOOK,
               css_paths=[THEME / "tokens.css", THEME / "pygments.css",
                          THEME / "book.css", THEME / "print.css"],
               out=DIST / "pyfly-by-example.pdf")
    print(f"Built {len(docs)} document(s) -> EPUB + PDF in {DIST}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
