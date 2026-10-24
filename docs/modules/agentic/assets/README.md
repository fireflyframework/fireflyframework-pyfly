# Agentic diagram provenance

Capability diagrams and the Manrope font are preserved from standalone Agentic
commit `f4234f9e481dd9c54e10048a2d67f7216a75f9e1`. They illustrate preserved source
algorithms and contracts. The PyFly site retains its own header, logo and typeface;
these source figures do not replace the PyFly brand system. Native application
ownership is documented in `../native-runtime.md`.

`provenance.json` maps all 36 public source Markdown guides and records their source
hashes. `fonts/OFL.txt` preserves the font license. `tools/brand-source/NOTICE.md`
and icon sources retain vendor attribution. `SOURCE-ASSETS.md` is the former asset
maintenance guide, preserved for provenance; its original root-relative commands
are historical.

Regenerate only the embedded capability figures and inherited artwork with:

```bash
python docs/modules/agentic/assets/tools/build_brand_assets.py
python docs/modules/agentic/assets/tools/build_brand_assets.py --check
```

The generator uses the preserved palette, measured geometry and vector sources.
Its output destination is adapted to this directory; it does not overwrite PyFly's
root brand assets. `diagrams.js` and `diagrams.css` add keyboard-operated expand/fit
controls to Material's actual Mermaid rendering, including narrow viewports.
