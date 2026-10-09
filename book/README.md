# pyfly by example

The English and Spanish editions follow Lumen through 18 chapters and five parts.
Appendix E adds the [native catalog webapp](../samples/webapp/README.md). Appendix F
adds [feature flags in Lumen](manuscript/96-appendix-f-feature-flags.md): an optional
wallet offer route, shared targeting fixture, runtime gates and operator workflow.

| Edition | PDF | EPUB |
|---|---|---|
| English | [PDF](https://github.com/fireflyframework/fireflyframework-pyfly/releases/download/books-2026.10.08/pyfly-by-example.pdf) | [EPUB](https://github.com/fireflyframework/fireflyframework-pyfly/releases/download/books-2026.10.08/pyfly-by-example.epub) |
| Spanish | [PDF](https://github.com/fireflyframework/fireflyframework-pyfly/releases/download/books-2026.10.08/pyfly-by-example-es.pdf) | [EPUB](https://github.com/fireflyframework/fireflyframework-pyfly/releases/download/books-2026.10.08/pyfly-by-example-es.epub) |

These links download the 2026-10-08 book edition, published independently of the
framework package. For other editions, open the [tagged releases](https://github.com/fireflyframework/fireflyframework-pyfly/releases)
and download the books attached to that tag.

## Sources and examples

`book.yaml` and `book.es.yaml` define chapter order and navigation. Manuscripts
live in `manuscript/` and `manuscript-es/`; `art/` and `theme/` supply shared artwork
and typography. Keep both editions aligned when changing behavior or examples.
The original Lumen chapters retain their historical version references; Appendices E
and F describe implementations in the accompanying source checkout.

- [Lumen](../samples/lumen/README.md): wallet, ledger, domain commands, and events.
- [Catalog](../samples/webapp/README.md): browser forms and administration sharing `Product`.
- [Webapp reference](../docs/modules/webapps.md): APIs, configuration, security, and provider contracts.
- [Feature Flags guide](../docs/modules/feature-flags.md): shared contract, APIs, configuration and troubleshooting.

## Rebuild both editions

From the repository root, use an isolated Python 3.12 environment:

```bash
uv venv book/.venv --python 3.12
uv pip install --python book/.venv/bin/python -r book/requirements.txt pytest
book/.venv/bin/python book/build/verify_code.py book/manuscript
book/.venv/bin/python book/build/verify_code.py book/manuscript-es
book/.venv/bin/python -m pytest book/tests -q
bash book/build/run.sh --out-dir book/release-output
bash book/build/run.sh --config book.es.yaml --out-dir book/release-output
book/.venv/bin/python book/build/release_assets.py create --dir book/release-output --commit "$(git rev-parse HEAD)"
book/.venv/bin/python book/build/release_assets.py verify --dir book/release-output --commit "$(git rev-parse HEAD)"
```

WeasyPrint requires native Pango libraries. On macOS install the Homebrew `pango`
package; the wrapper adds Homebrew's library directory to the process environment.
On Linux use the distribution's Pango packages. `--out-dir` directs the four
deliverables to an isolated directory; omitting it keeps the interactive default
of `book/dist/`. The release workflow builds both editions from the checked-out
tag, validates their structure, and publishes them with a SHA256 manifest that
records the source commit. Builds use the checked-in localized artwork; validate it with
`book/build/gen_cover.py` before publishing.

Listings use `::: listing path.py | Caption` and a closing `:::`. Figures use
`::: figure art/name.svg | Caption`. Fenced code also works inside callouts. Python
listing verification checks syntax; run the companion tests to verify behavior.
Inspect regenerated PDF pages and EPUB navigation before distributing an edition.

## Rebuild the documentation website

```bash
uv pip install --python book/.venv/bin/python -r requirements-docs.txt
book/.venv/bin/python scripts/build_site.py
book/.venv/bin/python -m mkdocs build --strict -d book/release-output/site-check
book/.venv/bin/python -m http.server --directory _site 8000
```

The local website is served at `http://localhost:8000/`; its reference documentation
is under `/docs/`. The site build copies the existing landing page and brand assets.
Book PDFs/EPUBs are separate deliverables linked from the repository and release pages.

## Branded front and back covers

The English and Spanish manifests select their own front/back SVG and PNG files
under `art/`. See [artwork provenance](art/PROVENANCE.md) for the canonical brand
kit source and delivered asset hashes. The SVG typography is outlined; the build
needs no author-machine fonts for the covers.

```bash
book/.venv/bin/python book/build/gen_cover.py
```

This validates all four SVG/PNG pairs without modifying them. Use an explicit
`--render` only to rasterize the checked-in SVGs again. The book build fails if a
configured PNG is missing. Covers fill the PDF trim without running text; EPUB
editions include accessible front/back documents and localized navigation.

Book-only editions can be published separately under `books-*` tags and must not
be marked as the latest framework release. Keep the book source commit and asset
checksums with the edition. A book-only edition does not change framework package
versions or replace the assets attached to an existing framework version.
