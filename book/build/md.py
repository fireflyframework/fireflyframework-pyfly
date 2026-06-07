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
from pathlib import Path
import markdown
from markdown.preprocessors import Preprocessor
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
                out.append(self.md.htmlStash.store(self._figure(mf["src"], mf["cap"])))
                i += 1
            elif ml:
                body, i = [], i + 1
                while i < len(lines) and lines[i].strip() != ":::":
                    body.append(lines[i]); i += 1
                i += 1
                out.append(self.md.htmlStash.store(
                    self._listing(ml["file"].strip(), ml["cap"], "\n".join(body))))
            else:
                out.append(lines[i]); i += 1
        return out

    def _figure(self, src: str, cap: str) -> str:
        svg = (self.base / src).read_text(encoding="utf-8").strip()
        svg = re.sub(r'^<\?xml[^>]*\?>\s*', "", svg)
        return f'<figure class="fig">{svg}<figcaption>{cap}</figcaption></figure>'

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

class PyflyExtension(Extension):
    def __init__(self, base: Path, **kw):
        self.base = base
        super().__init__(**kw)
    def extendMarkdown(self, md):
        md.preprocessors.register(_Directives(md, self.base), "pyfly_directives", 28)

def render_markdown(text: str, base: Path) -> str:
    md = markdown.Markdown(
        extensions=["extra", "admonition", "toc", "sane_lists", "codehilite",
                    PyflyExtension(base)],
        extension_configs={"codehilite": {"css_class": "code", "guess_lang": False}},
        output_format="html5",
    )
    return md.convert(text)
