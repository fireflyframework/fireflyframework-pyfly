"""Minimal, correct EPUB3 (OCF) assembler using only the standard library."""
from __future__ import annotations
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

_MEDIA = {".svg":"image/svg+xml",".png":"image/png",".jpg":"image/jpeg",
          ".jpeg":"image/jpeg",".css":"text/css",".woff2":"font/woff2",".woff":"font/woff"}

@dataclass
class Doc:
    id: str
    title: str
    xhtml_body: str
    in_nav: bool = True

@dataclass
class Asset:
    id: str
    href: str          # relative to OEBPS, e.g. "art/cover.png"
    data: bytes
    media_type: str
    properties: str = ""   # e.g. "cover-image"

@dataclass
class EpubBuilder:
    title: str
    author: str
    language: str
    identifier: str
    css: list[str] = field(default_factory=list)
    docs: list[Doc] = field(default_factory=list)
    assets: list[Asset] = field(default_factory=list)
    cover_asset_id: str | None = None

    def add_doc(self, d: Doc) -> None: self.docs.append(d)
    def add_asset(self, a: Asset) -> None: self.assets.append(a)

    def add_file(self, path: Path, href: str, aid: str, properties: str = "") -> Asset:
        a = Asset(id=aid, href=href, data=Path(path).read_bytes(),
                  media_type=_MEDIA[Path(href).suffix.lower()], properties=properties)
        self.assets.append(a)
        if properties == "cover-image": self.cover_asset_id = aid
        return a

    def _xhtml(self, d: Doc) -> str:
        links = "\n".join(f'<link rel="stylesheet" href="style{i}.css"/>'
                          for i in range(len(self.css)))
        return (f'<?xml version="1.0" encoding="utf-8"?>\n'
                f'<html xmlns="http://www.w3.org/1999/xhtml" '
                f'xmlns:epub="http://www.idpf.org/2007/ops" lang="{self.language}">\n'
                f'<head><meta charset="utf-8"/><title>{escape(d.title)}</title>\n{links}\n</head>\n'
                f'<body><section class="chapter">{d.xhtml_body}</section></body>\n</html>\n')

    def _nav(self) -> str:
        items = "\n".join(f'<li><a href="{d.id}.xhtml">{escape(d.title)}</a></li>'
                          for d in self.docs if d.in_nav)
        return ('<?xml version="1.0" encoding="utf-8"?>\n'
                '<html xmlns="http://www.w3.org/1999/xhtml" '
                'xmlns:epub="http://www.idpf.org/2007/ops"><head><meta charset="utf-8"/>'
                '<title>Contents</title></head><body>'
                f'<nav epub:type="toc" id="toc"><h1>Contents</h1><ol>{items}</ol></nav>'
                '</body></html>')

    def _opf(self) -> str:
        modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        man = ['<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>']
        for i in range(len(self.css)):
            man.append(f'<item id="css{i}" href="style{i}.css" media-type="text/css"/>')
        for d in self.docs:
            man.append(f'<item id="{d.id}" href="{d.id}.xhtml" media-type="application/xhtml+xml"/>')
        for a in self.assets:
            props = f' properties="{a.properties}"' if a.properties else ""
            man.append(f'<item id="{a.id}" href="{a.href}" media-type="{a.media_type}"{props}/>')
        spine = "".join(f'<itemref idref="{d.id}"/>' for d in self.docs)
        cover_meta = (f'<meta name="cover" content="{self.cover_asset_id}"/>'
                      if self.cover_asset_id else "")
        return (f'<?xml version="1.0" encoding="utf-8"?>\n'
                f'<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
                f'unique-identifier="bookid">\n'
                f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                f'<dc:identifier id="bookid">{escape(self.identifier)}</dc:identifier>'
                f'<dc:title>{escape(self.title)}</dc:title>'
                f'<dc:creator>{escape(self.author)}</dc:creator>'
                f'<dc:language>{self.language}</dc:language>'
                f'<meta property="dcterms:modified">{modified}</meta>{cover_meta}</metadata>'
                f'<manifest>{"".join(man)}</manifest>'
                f'<spine>{spine}</spine></package>')

    def build(self, out: Path) -> Path:
        out = Path(out); out.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(out, "w") as z:
            # 1) mimetype FIRST and STORED
            zi = zipfile.ZipInfo("mimetype")
            zi.compress_type = zipfile.ZIP_STORED
            z.writestr(zi, "application/epub+zip")
            # 2) container
            z.writestr("META-INF/container.xml",
                '<?xml version="1.0" encoding="utf-8"?>\n'
                '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                '<rootfiles><rootfile full-path="OEBPS/content.opf" '
                'media-type="application/oebps-package+xml"/></rootfiles></container>',
                zipfile.ZIP_DEFLATED)
            # 3) css, docs, nav, opf
            for i, css in enumerate(self.css):
                z.writestr(f"OEBPS/style{i}.css", css, zipfile.ZIP_DEFLATED)
            for d in self.docs:
                z.writestr(f"OEBPS/{d.id}.xhtml", self._xhtml(d), zipfile.ZIP_DEFLATED)
            z.writestr("OEBPS/nav.xhtml", self._nav(), zipfile.ZIP_DEFLATED)
            z.writestr("OEBPS/content.opf", self._opf(), zipfile.ZIP_DEFLATED)
            # 4) assets
            for a in self.assets:
                z.writestr(f"OEBPS/{a.href}", a.data, zipfile.ZIP_DEFLATED)
        return out
