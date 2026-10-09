"""Validate canonical cover artwork; --render regenerates PNGs from outlined SVGs.

The source of the artwork is documented in book/art/PROVENANCE.md. This command
never creates a design or substitutes system fonts.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from xml.etree import ElementTree

from PIL import Image

ART = Path(__file__).resolve().parents[1] / "art"
NAMES = ("cover", "cover-es", "back-cover", "back-cover-es")
WIDTH, HEIGHT = 1500, 1850


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--render", action="store_true", help="Rasterize the validated SVG sources.")
    args = parser.parse_args(argv)
    for name in NAMES:
        path = ART / f"{name}.svg"
        root = ElementTree.parse(path).getroot()
        if (root.tag != "{http://www.w3.org/2000/svg}svg"
                or tuple(float(n) for n in root.get("viewBox", "").split()) != (0, 0, WIDTH, HEIGHT)
                or root.get("width") != str(WIDTH) or root.get("height") != str(HEIGHT)):
            raise ValueError(f"Expected 1500 x 1850 canonical SVG: {path}")
        if any(node.tag == "{http://www.w3.org/2000/svg}text" for node in root.iter()):
            raise ValueError(f"Cover typography must be outlined, not font-dependent: {path}")
    if args.render:
        import cairosvg
        for name in NAMES:
            target = ART / f"{name}.png"
            temporary = target.with_suffix(".png.tmp")
            try:
                cairosvg.svg2png(url=str(ART / f"{name}.svg"), write_to=str(temporary),
                                output_width=WIDTH, output_height=HEIGHT)
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
    for name in NAMES:
        path = ART / f"{name}.png"
        with Image.open(path) as image:
            if image.format != "PNG" or image.size != (WIDTH, HEIGHT):
                raise ValueError(f"Expected 1500 x 1850 canonical PNG: {path}")
            image.verify()
    print("Verified four localized front/back cover pairs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
