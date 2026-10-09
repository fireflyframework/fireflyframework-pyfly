# Book artwork provenance

The front and back covers belong to the shared Firefly Framework book collection,
prepared on 2026-10-08. Canonical delivery: `Framework-Brand-Kit/11-Books/python/`.
They use the official Firefly Framework identity (A2 treatment) and Manrope
letterforms converted to vector paths. They contain no live font dependency.

The source SVGs and delivered PNGs are 1500 × 1850 pixels, matching the book's
7.5 × 9.25 inch trim. English uses `cover` and `back-cover`; Spanish uses
`cover-es` and `back-cover-es`. Both are full pages in PDF and explicit bookends
in the EPUB reading order. Edition manifests supply accessible alternative text.

`book/build/gen_cover.py` validates this canonical set without changing it.
An explicit `--render` recreates the PNGs from these SVGs with CairoSVG; it never
recreates the retired cover design or substitutes machine-local fonts. Raster
bytes can vary between renderers, so refresh the hashes below if replacing the
delivered PNGs. Publication checksums identify the actual PDF/EPUB bytes.

## Delivered asset SHA-256

| Asset | SHA-256 |
|---|---|
| `cover.svg` | `7f77783fc98641090ff12821c65cad3dd143abb598c687564bbb2b21698c3f75` |
| `cover.png` | `f8c0c553dc4441b00fa6fd413850826582b4f87ca7c6958ac30bbd09800b3326` |
| `cover-es.svg` | `25ff67dc70741db025bff262d0d3f31587d0d66ce4a6fe3f934a0d002a4cfd83` |
| `cover-es.png` | `14c1698e6899ec1ad5777ecfa5ac6e81e0836eee21fab7666d3d204817fa748e` |
| `back-cover.svg` | `03ce4a2a4ce8e6cd6ef19a082a7117caec43e72c81fdc0798ab1012fae831de9` |
| `back-cover.png` | `c2b839fdd64379b81a2e303bc9d54cb44ae21eaec6cc1e135580a9528ba00ee4` |
| `back-cover-es.svg` | `165ac7a9676fc93d1ee8bb7c406a947a8984ae248ced46d07fd2659adfae8947` |
| `back-cover-es.png` | `cb7879a394f362a20ccef4af84dac191be52c92c8b4b2b202728f825564c88d8` |

## Documentation and interior diagram identity

The 2026-10-08 identity pass applies shared ink (`#10110f`), warm neutral
(`#f3f1eb` / `#dedbd2`) and amber (`#ffb34a`) surfaces. Dark amber (`#8a5714`)
is reserved for readable lines and text on white. Existing blue, green and rust
status/callout colors and third-party marks retain their semantic distinction.

`diagram-branding.json` inventories the diagrams and records SHA-256 fingerprints
of their original non-paint structure. These fingerprints cover every technical
label, path, arrow and layout attribute. Only paint, the old decorative raster
mark and the added footer are excluded. The original viewBox is recorded too.
The footer adds 7.8% of the original width below the content; its outlined family
lockup is embedded with unique SVG IDs and does not cover or scale any original
shape. Screen readers retain the diagram's original title/description.

The canonical lockup is `docs/assets/pyfly-logo-light.svg`,
from `Framework-Brand-Kit/12-Frameworks/python/`.
The documentation favicon and small diagram marks use the official kit's
`02-Icons/favicon.svg`. No legacy snake/insect raster is regenerated.

Run `book/.venv/bin/python book/build/brand_diagrams.py --check` to validate
identity, geometry, text and mirrors without writing. Omit `--check` to apply
the idempotent palette/footer pass to a reviewed original source. An intentional
technical diagram edit requires review and updating its fingerprint; never reset
fingerprints merely to make a failing check pass.

Book interiors use the same ink, paper and amber theme. Blue informational,
green tip, rust warning and native Spring/Laravel callout colors remain semantic.

The six public diagrams retain their native layout generator in
`assets/tools/build_brand_assets.py`; it copies the canonical banner, regenerates
the existing geometry, then invokes the identity pass. The other 43 diagrams
are hand-authored native SVG sources.

For PDF only, `book/build/pdf.py` rasterizes the small canonical footer lockup
at 800 pixels wide before WeasyPrint renders it. This preserves the approved
gradient-y and clipped paths that WeasyPrint otherwise simplifies. Diagram
labels and technical shapes remain selectable text and vectors. Public SVGs
and EPUBs keep the outlined vector logo. Gradients whose stops are the same
color use identical solid paint so WeasyPrint does not drop header backgrounds.
