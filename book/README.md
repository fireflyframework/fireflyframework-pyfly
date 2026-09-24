# PyFly by Example

The English and Spanish editions follow Lumen through 18 chapters and five parts.
Appendix E adds the [native catalog webapp](../samples/webapp/README.md): HTML pages,
forms, named-route/static helpers, custom errors, and administration of the same
existing SQLAlchemy entities or Beanie documents.

| Edition | PDF | EPUB |
|---|---|---|
| English | [PDF](dist/pyfly-by-example.pdf) | [EPUB](dist/pyfly-by-example.epub) |
| Spanish | [PDF](dist/pyfly-by-example-es.pdf) | [EPUB](dist/pyfly-by-example-es.epub) |

## Sources and examples

`book.yaml` and `book.es.yaml` define chapter order and navigation. Manuscripts
live in `manuscript/` and `manuscript-es/`; `art/` and `theme/` supply shared artwork
and typography. Keep both editions aligned when changing behavior or examples.
The original Lumen chapters retain their historical version references; Appendix E
describes the implementation in the accompanying source checkout.

- [Lumen](../samples/lumen/README.md): wallet, ledger, domain commands, and events.
- [Catalog](../samples/webapp/README.md): browser forms and administration sharing `Product`.
- [Webapp reference](../docs/modules/webapps.md): APIs, configuration, security, and provider contracts.

## Rebuild both editions

From the repository root, use an isolated Python 3.12 environment:

```bash
uv venv book/.venv --python 3.12
uv pip install --python book/.venv/bin/python -r book/requirements.txt pytest
book/.venv/bin/python book/build/verify_code.py book/manuscript
book/.venv/bin/python book/build/verify_code.py book/manuscript-es
book/.venv/bin/python -m pytest book/tests -q
bash book/build/run.sh
bash book/build/run.sh --config book.es.yaml
```

WeasyPrint requires native Pango libraries. On macOS install the Homebrew `pango`
package; the wrapper adds Homebrew's library directory to the process environment.
On Linux use the distribution's Pango packages. The builder regenerates the four
deliverables in `book/dist/`, including contents and navigation; it does not publish
or create a release. Existing covers are reused when their sources have not changed.

Listings use `::: listing path.py | Caption` and a closing `:::`. Figures use
`::: figure art/name.svg | Caption`. Fenced code also works inside callouts. Python
listing verification checks syntax; run the companion tests to verify behavior.
Inspect regenerated PDF pages and EPUB navigation before distributing an edition.

## Rebuild the documentation website

```bash
uv pip install --python book/.venv/bin/python -r requirements-docs.txt
book/.venv/bin/python scripts/build_site.py
book/.venv/bin/python -m mkdocs build --strict -d /tmp/pyfly-docs-check
book/.venv/bin/python -m http.server --directory _site 8000
```

The local website is served at `http://localhost:8000/`; its reference documentation
is under `/docs/`. The site build copies the existing landing page and brand assets.
Book PDFs/EPUBs are separate deliverables linked from the repository and release pages.
