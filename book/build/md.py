"""Markdown -> HTML for *PyFly by Example*.

Custom block directives on top of python-markdown:
  ::: figure <svg-path> | <caption>          (single line; inlines the SVG)
  ::: listing <file-label> | <caption>       (block; code lines until a lone ':::')
      <code...>
  :::
Plain ``` fences still work via codehilite. Callouts use the admonition
extension (note/tip/warning + custom 'spring').
"""
from __future__ import annotations
import re
import base64
import html.entities as _htmlent
from pathlib import Path
import markdown
from markdown.preprocessors import Preprocessor
from markdown.postprocessors import Postprocessor
from markdown.extensions import Extension
from pygments import highlight
from pygments.lexers import get_lexer_by_name, guess_lexer
from pygments.util import ClassNotFound
from pygments.formatters import HtmlFormatter

_FMT = HtmlFormatter(nowrap=True)  # tokens only; we supply <pre class="code">
_EXT = {"py":"python","yaml":"yaml","yml":"yaml","toml":"toml","json":"json",
        "sh":"bash","bash":"bash","sql":"sql","xml":"xml","html":"html","txt":"text"}

class _Directives(Preprocessor):
    FIG = re.compile(r'^:::\s*figure\s+(?P<src>\S+)\s*\|\s*(?P<cap>.+?)\s*$')
    LST = re.compile(r'^:::\s*listing\s+(?P<file>[^|]+?)\s*(?:\|\s*(?P<cap>.+?))?\s*$')

    def __init__(self, md, base: Path):
        super().__init__(md)
        self.base = Path(base)

    def run(self, lines):
        out, i = [], 0
        while i < len(lines):
            mf, ml = self.FIG.match(lines[i]), self.LST.match(lines[i])
            if mf:
                # surround with blank lines so the block HTML never lands inside a <p>
                out.extend(["", self.md.htmlStash.store(self._figure(mf["src"], mf["cap"])), ""])
                i += 1
            elif ml:
                body, i = [], i + 1
                while i < len(lines) and lines[i].strip() != ":::":
                    body.append(lines[i]); i += 1
                i += 1
                out.extend(["", self.md.htmlStash.store(
                    self._listing(ml["file"].strip(), ml["cap"], "\n".join(body))), ""])
            else:
                out.append(lines[i]); i += 1
        return out

    def _figure(self, src: str, cap: str) -> str:
        p = self.base / src
        if p.suffix.lower() == ".svg":
            svg = p.read_text(encoding="utf-8").strip()
            inner = re.sub(r'^<\?xml[^>]*\?>\s*', "", svg)
        else:  # raster image -> embed as a data URI so it travels into EPUB + PDF
            mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                    ".webp": "image/webp", ".gif": "image/gif"}.get(p.suffix.lower(), "image/png")
            data = base64.b64encode(p.read_bytes()).decode("ascii")
            inner = f'<img class="fig-img" alt="" src="data:{mime};base64,{data}"/>'
        return f'<figure class="fig">{inner}<figcaption>{cap}</figcaption></figure>'

    def _listing(self, file_label: str, cap: str | None, code: str) -> str:
        lang = _EXT.get(file_label.rsplit(".", 1)[-1].lower(), "python") if "." in file_label else "python"
        try:
            lexer = get_lexer_by_name(lang)
        except ClassNotFound:
            lexer = guess_lexer(code)
        body = highlight(code, lexer, _FMT)
        cap_html = f'<div class="lcap">{cap}</div>' if cap else ""
        return (f'<div class="listing"><span class="filetab">{file_label}</span>'
                f'<pre class="code">{body}</pre>{cap_html}</div>')

# Professional inline SVG icons injected into callout titles (no emoji).
_ADM_ICON = {
    "note": '<svg class="adm-ico" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" aria-hidden="true">'
            '<circle cx="10" cy="10" r="8.3" fill="none" stroke="#1f6fd6" stroke-width="1.6"/>'
            '<circle cx="10" cy="6.1" r="1.25" fill="#1f6fd6"/>'
            '<rect x="9.1" y="8.7" width="1.8" height="5.6" rx="0.9" fill="#1f6fd6"/></svg>',
    "tip": '<svg class="adm-ico" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" aria-hidden="true">'
           '<path d="M10 2.4a5.6 5.6 0 0 0-3.3 10.1c.45.33.72.8.78 1.32l.09.78h4.86l.09-.78'
           'c.06-.52.33-.99.78-1.32A5.6 5.6 0 0 0 10 2.4z" fill="none" stroke="#2f8f3f" stroke-width="1.5"/>'
           '<path d="M8 17.2h4M8.7 18.7h2.6" stroke="#2f8f3f" stroke-width="1.4" stroke-linecap="round"/></svg>',
    "warning": '<svg class="adm-ico" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" aria-hidden="true">'
               '<path d="M10 3l7.3 12.6H2.7L10 3z" fill="none" stroke="#c2410c" stroke-width="1.6" stroke-linejoin="round"/>'
               '<rect x="9.1" y="8.2" width="1.8" height="4.4" rx="0.9" fill="#c2410c"/>'
               '<circle cx="10" cy="13.9" r="1.05" fill="#c2410c"/></svg>',
    "spring": '<svg class="adm-ico" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" aria-hidden="true">'
              '<path d="M4.5 15.5c0-5.5 4-9.5 11-9.5-.5 5.5-4.5 9.5-11 9.5z" fill="none" '
              'stroke="#43b02a" stroke-width="1.5" stroke-linejoin="round"/>'
              '<path d="M6 14.2c2.6-3.2 5.4-5.2 8.4-6.1" stroke="#43b02a" stroke-width="1.3" '
              'fill="none" stroke-linecap="round"/></svg>',
}

class _AdmonitionIcons(Postprocessor):
    """Inject an inline SVG icon at the start of each callout title."""
    _RE = re.compile(r'(<div class="admonition (?P<t>note|tip|warning|spring)">\s*<p class="admonition-title">)')
    def run(self, text: str) -> str:
        return self._RE.sub(lambda m: m.group(1) + _ADM_ICON.get(m.group("t"), ""), text)

class PyflyExtension(Extension):
    def __init__(self, base: Path, **kw):
        self.base = base
        super().__init__(**kw)
    def extendMarkdown(self, md):
        md.preprocessors.register(_Directives(md, self.base), "pyfly_directives", 28)
        md.postprocessors.register(_AdmonitionIcons(md), "pyfly_adm_icons", 5)

_XML_SAFE_ENTITIES = {"amp", "lt", "gt", "quot", "apos"}

def _to_xml_entities(s: str) -> str:
    """Convert HTML named entities (e.g. &nbsp;) to numeric refs so the output is
    well-formed XML for EPUB3 XHTML (XML predefines only amp/lt/gt/quot/apos)."""
    def repl(m: "re.Match[str]") -> str:
        name = m.group(1)
        if name in _XML_SAFE_ENTITIES:
            return m.group(0)
        cp = _htmlent.name2codepoint.get(name)
        return f"&#{cp};" if cp is not None else m.group(0)
    return re.sub(r"&([a-zA-Z][a-zA-Z0-9]*);", repl, s)


def render_markdown(text: str, base: Path) -> str:
    md = markdown.Markdown(
        extensions=["extra", "admonition", "sane_lists", "codehilite",
                    PyflyExtension(base)],
        extension_configs={"codehilite": {"css_class": "code", "guess_lang": False}},
        output_format="xhtml",
    )
    return _to_xml_entities(md.convert(text))
