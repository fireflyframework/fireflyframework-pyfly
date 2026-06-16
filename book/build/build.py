"""Build *PyFly by Example* into EPUB + PDF from a book manifest.

Defaults to ``book.yaml`` (English). Pass ``--config book.es.yaml`` to build the
Spanish edition; each manifest names its own ``manuscript_dir``, ``language``,
localized ``labels`` (e.g. the Contents heading) and ``output_basename``.

    book/build/run.sh                       # English  -> pyfly-by-example.{epub,pdf}
    book/build/run.sh --config book.es.yaml # Spanish  -> pyfly-by-example-es.{epub,pdf}
"""
from __future__ import annotations
import argparse
import re
import sys
from pathlib import Path
from xml.sax.saxutils import escape
import yaml  # PyYAML ships with the framework env; ensure installed in book/.venv

sys.path.insert(0, str(Path(__file__).resolve().parent))
from md import render_markdown          # noqa: E402
from epub import EpubBuilder, Doc       # noqa: E402
from pdf import render_pdf              # noqa: E402

BOOK = Path(__file__).resolve().parents[1]
THEME = BOOK / "theme"
DIST = BOOK / "dist"


def _split_part(part_title: str) -> tuple[str, str]:
    """'Part I — Foundations' -> ('Part I', 'Foundations').

    Splits on an em/en dash (with optional spaces) or a plain ' - '. Falls back
    to the whole string as the title with an empty eyebrow when no dash is found.
    """
    m = re.match(r"\s*(.+?)\s*[—–-]\s*(.+?)\s*$", part_title)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return "", part_title.strip()


def _items_from_manifest(cfg: dict, man: Path, *, contents_label: str) -> list[dict]:
    """Ordered build items, each tagged ``kind`` and carrying the metadata that
    kind needs. Consumed by BOTH the EPUB and PDF assemblers.

    kinds:
      front    -> {id, title, path, in_nav}
      toc      -> {id, title}                     (Contents page; content generated)
      divider  -> {id, eyebrow, ptitle, part}     (full-page part opener)
      chapter  -> {id, title, num, path, part}    (a manuscript chapter)
    """
    items: list[dict] = []
    # 1) front matter
    for fm in cfg.get("front", []):
        p = man / fm["file"]
        if not p.exists():
            continue
        items.append({
            "kind": "front",
            "id": fm["id"],
            "title": fm.get("title", fm["id"].title()),
            "path": str(p),
            "in_nav": bool(fm.get("nav", True)) and "title" in fm,
        })
    # 2) Contents page — after front matter, before Part I
    items.append({"kind": "toc", "id": "toc", "title": contents_label})
    # 3) parts: a divider then each chapter
    for part in cfg.get("parts", []):
        ptitle_full = part["title"]
        eyebrow, ptitle = _split_part(ptitle_full)
        chapters = [ch for ch in part["chapters"] if (man / ch["file"]).exists()]
        if not chapters:
            continue
        # stable divider id from the eyebrow, e.g. "Part I" -> "part-i"
        slug = re.sub(r"[^a-z0-9]+", "-", eyebrow.lower()).strip("-") if eyebrow else ""
        did = slug if slug.startswith("part") else f"part-{slug or len(items)}"
        items.append({
            "kind": "divider",
            "id": did,
            "eyebrow": eyebrow,
            "ptitle": ptitle,
            "part": ptitle_full,
        })
        for ch in chapters:
            items.append({
                "kind": "chapter",
                "id": ch["id"],
                "title": (f'{ch["num"]}. {ch["title"]}' if ch.get("num") not in (None, "") else ch["title"]),
                "num": ch["num"],
                "path": str(man / ch["file"]),
                "part": ptitle_full,
            })
    return items


def _toc_html(items: list[dict], *, href_fmt: str, label: str) -> str:
    """Generate the Contents body. ``href_fmt`` formats a chapter id into a link
    target: '#{cid}' for the single-document PDF, '{cid}.xhtml' for the EPUB."""
    parts: list[str] = []
    cur: str | None = None
    open_group = False
    for it in items:
        if it["kind"] == "divider":
            if open_group:
                parts.append("</ol></div>")
            eyebrow = f'<span class="toc-part-eyebrow">{escape(it["eyebrow"])}</span> ' \
                if it["eyebrow"] else ""
            parts.append('<div class="toc-part-group">'
                         f'<h2 class="toc-part-title">{eyebrow}{escape(it["ptitle"])}</h2>'
                         '<ol class="toc-chapters">')
            open_group = True
            cur = it["part"]
        elif it["kind"] == "chapter" and open_group:
            href = href_fmt.format(cid=it["id"])
            parts.append(f'<li><a class="toc-link" href="{escape(href)}">'
                         f'{escape(it["title"])}</a></li>')
    if open_group:
        parts.append("</ol></div>")
    body = "".join(parts)
    return f'<h1 class="chtitle">{escape(label)}</h1>{body}'


def _divider_html(eyebrow: str, ptitle: str) -> str:
    eb = f'<span class="eyebrow part-eyebrow">{escape(eyebrow)}</span>' if eyebrow else ""
    return (f'<div class="part-divider-inner">{eb}'
            f'<h1 class="part-title">{escape(ptitle)}</h1></div>')


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build PyFly by Example (EPUB + PDF).")
    ap.add_argument("--config", default="book.yaml",
                    help="Manifest file under book/ (default: book.yaml).")
    ap.add_argument("--out", default=None,
                    help="Output basename (default: manifest 'output_basename' or 'pyfly-by-example').")
    args = ap.parse_args(argv)

    cfg = yaml.safe_load((BOOK / args.config).read_text())
    man = BOOK / cfg.get("manuscript_dir", "manuscript")
    contents_label = cfg.get("labels", {}).get("contents", "Contents")
    out_base = args.out or cfg.get("output_basename") or "pyfly-by-example"

    css_text = [(THEME / "book.css").read_text(), (THEME / "tokens.css").read_text(),
                (THEME / "pygments.css").read_text()]
    items = _items_from_manifest(cfg, man, contents_label=contents_label)

    # ---- EPUB ----
    epub = EpubBuilder(title=cfg["title"], author=cfg["author"], language=cfg["language"],
                       identifier=cfg["identifier"], css=css_text)
    cover_png = BOOK / cfg["cover_png"]
    if cover_png.exists():
        epub.add_file(cover_png, "art/cover.png", "cover-img", properties="cover-image")
    for it in items:
        if it["kind"] == "toc":
            body = _toc_html(items, href_fmt="{cid}.xhtml", label=contents_label)
            epub.add_doc(Doc(id=it["id"], title=it["title"], xhtml_body=body,
                             in_nav=True, kind="toc"))
        elif it["kind"] == "divider":
            body = _divider_html(it["eyebrow"], it["ptitle"])
            epub.add_doc(Doc(id=it["id"], title=it["part"], xhtml_body=body,
                             in_nav=False, kind="divider", part=it["part"]))
        else:  # front | chapter
            body = render_markdown(Path(it["path"]).read_text(encoding="utf-8"), BOOK)
            epub.add_doc(Doc(id=it["id"], title=it["title"], xhtml_body=body,
                             in_nav=it.get("in_nav", True), kind=it["kind"],
                             part=it.get("part"), num=it.get("num")))
    DIST.mkdir(exist_ok=True)
    epub.build(DIST / f"{out_base}.epub")

    # ---- PDF (single concatenated document) ----
    parts_html: list[str] = []
    if cover_png.exists():
        parts_html.append(f'<div class="cover-page"><img src="{cfg["cover_png"]}"/></div>')
    for it in items:
        if it["kind"] == "toc":
            body = _toc_html(items, href_fmt="#{cid}", label=contents_label)
            parts_html.append(f'<section class="toc" id="{it["id"]}">{body}</section>')
        elif it["kind"] == "divider":
            body = _divider_html(it["eyebrow"], it["ptitle"])
            parts_html.append(f'<section class="part-divider" id="{it["id"]}">{body}</section>')
        else:  # front | chapter
            body = render_markdown(Path(it["path"]).read_text(encoding="utf-8"), BOOK)
            parts_html.append(f'<section class="chapter" id="{it["id"]}">{body}</section>')
    full = ("<!DOCTYPE html><html><head><meta charset='utf-8'></head><body>"
            + "\n".join(parts_html) + "</body></html>")
    render_pdf(full, base_url=BOOK,
               css_paths=[THEME / "tokens.css", THEME / "pygments.css",
                          THEME / "book.css", THEME / "print.css"],
               out=DIST / f"{out_base}.pdf")
    n = sum(1 for it in items if it["kind"] in ("front", "chapter"))
    print(f"[{cfg['language']}] Built {n} document(s) + TOC + "
          f"{sum(1 for it in items if it['kind']=='divider')} part divider(s) "
          f"-> {out_base}.epub + {out_base}.pdf in {DIST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
