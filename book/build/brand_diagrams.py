"""Apply the approved identity without moving diagram content.

The manifest records the original non-paint structure. Canonical outlined logos
are embedded, never fetched. Reapplying the operation is byte-idempotent.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "book/art/diagram-branding.json"
SVG = "{http://www.w3.org/2000/svg}"
ET.register_namespace("", "http://www.w3.org/2000/svg")
ET.register_namespace("xlink", "http://www.w3.org/1999/xlink")
PAINT = {"fill", "stroke", "stop-color", "flood-color", "color"}


def structure(source: str) -> str:
    """Fingerprint every original label, path and layout attribute, excluding paint."""
    root = ET.fromstring(source)
    def visit(node, top=False):
        if node.get("data-framework-brand") or node.get("data-brand-decoration") or node.tag == SVG + "image":
            return None
        attrs = {key: value for key, value in node.attrib.items() if key not in PAINT}
        if top:
            for key in ("height", "viewBox", "data-original-view-box"):
                attrs.pop(key, None)
        return [node.tag, sorted(attrs.items()), (node.text or "").strip(),
                [result for child in node if (result := visit(child)) is not None]]
    return hashlib.sha256(json.dumps(visit(root, True), ensure_ascii=False).encode()).hexdigest()


def embedded_asset(path: Path, prefix: str, **attrs) -> str:
    root = ET.fromstring(path.read_text())
    for node in list(root):
        if node.tag in (SVG + "title", SVG + "desc"):
            root.remove(node)
    ids = {node.get("id"): prefix + node.get("id") for node in root.iter() if node.get("id")}
    for node in root.iter():
        for key, value in list(node.attrib.items()):
            if key == "id":
                node.set(key, ids[value])
            else:
                for old, new in ids.items():
                    value = value.replace("#" + old + ")", "#" + new + ")")
                    if value == "#" + old:
                        value = "#" + new
                node.set(key, value)
    root.attrib.update({key.replace("_", "-"): str(value) for key, value in attrs.items()})
    return ET.tostring(root, encoding="unicode")


def brand(source: str, relative: str, config: dict) -> str:
    root = ET.fromstring(source)
    # WeasyPrint 69 drops fills whose gradient stops collapse to one color.
    # A solid paint is visually identical and keeps white header labels legible.
    for gradient in root.iter(SVG + "linearGradient"):
        stops = list(gradient)
        colors = {stop.get("stop-color") for stop in stops}
        if len(colors) == 1 and None not in colors and all(stop.get("stop-opacity", "1") == "1" for stop in stops):
            color = colors.pop()
            source = source.replace(f'="url(#{gradient.get("id")})"', f'="{color}"')
    if root.find(f"{SVG}g[@data-framework-brand='2026-10-08']") is not None:
        if "<!-- framework-logo:start -->" not in source:
            start = source.index("<svg", source.index('data-framework-brand="2026-10-08"'))
            end = source.rfind("</svg>", 0, source.rfind("</g>")) + len("</svg>")
            source = (source[:start] + "<!-- framework-logo:start -->" + source[start:end]
                      + "<!-- framework-logo:end -->" + source[end:])
        return source
    x, y, width, height = map(float, root.get("viewBox").split())
    if x or y:
        raise ValueError("The diagram must use a zero-origin viewBox")
    prefix = "brand-" + hashlib.sha256(relative.encode()).hexdigest()[:10] + "-"
    palette = config["palette"]
    source = re.sub(r"#[0-9a-fA-F]{6}\b", lambda match: palette.get(match[0].lower(), match[0]), source)
    counter = 0
    def replace_icon(match):
        nonlocal counter
        counter += 1
        image = ET.fromstring(match[0])
        return embedded_asset(ROOT / config["mark"], prefix + str(counter) + "-",
                              data_brand_decoration="official-mark", aria_hidden="true",
                              **{key: image.get(key) for key in ("x", "y", "width", "height")})
    source = re.sub(r"<image\b[^>]*?/>|<image\b[^>]*?>.*?</image>", replace_icon, source, flags=re.S)
    footer = round(width * .078, 2)
    logo_height = round(width * .068, 2)
    logo = ET.parse(ROOT / config["logo"]).getroot()
    logo_width = round(logo_height * float(logo.get("width")) / float(logo.get("height")), 2)
    asset = embedded_asset(ROOT / config["logo"], prefix + "logo-", x=round(width-logo_width-12, 2),
                           y=round(height + (footer-logo_height)/2, 2), width=logo_width, height=logo_height,
                           aria_hidden="true")
    panel = (f'<g data-framework-brand="2026-10-08" aria-hidden="true">'
             f'<rect x="0" y="{height:g}" width="{width:g}" height="{footer:g}" fill="#ffffff"/>'
             f'<path d="M12 {height+2:g}H{width-12:g}" stroke="#dedbd2" stroke-width="1"/>'
             f'<path d="M12 {height+2:g}H{width*.1:g}" stroke="#ffb34a" stroke-width="2"/>'
             + '<!-- framework-logo:start -->' + asset + '<!-- framework-logo:end --></g>')
    match = re.search(r"<svg\b[^>]*>", source)
    opening = match[0]
    total_height = round(height + footer, 2)
    opening = re.sub(r'viewBox="[^"]+"', f'viewBox="0 0 {width:g} {total_height:g}"', opening)
    if 'height="' in opening:
        opening = re.sub(r'height="[^"]+"', f'height="{total_height:g}"', opening)
    opening = opening[:-1] + f' data-original-view-box="0 0 {width:g} {height:g}">'
    source = source[:match.start()] + opening + source[match.end():]
    return brand(source.rsplit("</svg>", 1)[0] + "\n" + panel + "\n</svg>\n", relative, config)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Validate without writing.")
    args = parser.parse_args(argv)
    config = json.loads(MANIFEST.read_text())
    for relative, record in config["diagrams"].items():
        path = ROOT / relative
        original = path.read_text()
        if structure(original) != record["structure"]:
            raise ValueError(f"Diagram content/layout changed; review its manifest entry: {relative}")
        result = brand(original, relative, config)
        if structure(result) != record["structure"]:
            raise ValueError(f"Branding moved or changed diagram content: {relative}")
        if args.check and result != original:
            raise ValueError(f"Diagram needs branding: {relative}")
        if not args.check and result != original:
            path.write_text(result)
        for mirror in record.get("mirrors", []):
            mirror_path = ROOT / mirror
            if args.check and mirror_path.read_text() != result:
                raise ValueError(f"Diagram mirror differs: {mirror}")
            if not args.check and mirror_path.read_text() != result:
                mirror_path.write_text(result)
    print(f"Verified {len(config['diagrams'])} diagram layouts and canonical branding.")


if __name__ == "__main__":
    main()
