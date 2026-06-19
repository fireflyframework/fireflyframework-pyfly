"""Generate book/art/cover.svg and cover.png — premium redesign."""
from __future__ import annotations
import base64
import math
import os
from pathlib import Path

ART = Path(__file__).resolve().parents[1] / "art"
W, H = 1500, 2100  # 7.5 × 9.25 in at 200 dpi

# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------
INK       = "#16331a"   # deep forest — background
DEEP      = "#1e4620"   # slightly lighter ink for layering
GREEN     = "#43b02a"   # brand green
GREEN_MID = "#2c8a1c"   # mid green
GREEN_DIM = "#255e17"   # dim green — subtle network lines
LIGHT     = "#eaf6df"   # very light green — body text
WHITE     = "#ffffff"
AMBER     = "#ffc24b"
MUTED     = "#8dbd7a"   # muted green — secondary text
MUTED_LT  = "#b5d9a0"   # lighter muted — subtitle


# ---------------------------------------------------------------------------
# Network motif helpers
# ---------------------------------------------------------------------------
def hex_points(cx: float, cy: float, r: float) -> str:
    """Return SVG polygon points string for a flat-top hexagon."""
    pts = []
    for i in range(6):
        angle = math.radians(30 + 60 * i)  # flat-top orientation
        pts.append(f"{cx + r * math.cos(angle):.2f},{cy + r * math.sin(angle):.2f}")
    return " ".join(pts)


def node(cx: float, cy: float, r: float, fill: str, stroke: str,
         stroke_w: float = 2.0, opacity: float = 1.0) -> str:
    pts = hex_points(cx, cy, r)
    op = f' opacity="{opacity}"' if opacity < 1.0 else ""
    return (
        f'<polygon points="{pts}" fill="{fill}" '
        f'stroke="{stroke}" stroke-width="{stroke_w}"{op}/>'
    )


def quad_path(x1: float, y1: float, x2: float, y2: float) -> str:
    """Smooth quadratic bezier between two points, gently curved outward."""
    mx, my = (x1 + x2) / 2, (y1 + y2) / 2
    dx, dy = x2 - x1, y2 - y1
    cpx = mx - dy * 0.18
    cpy = my + dx * 0.18
    return f"M{x1:.1f},{y1:.1f} Q{cpx:.1f},{cpy:.1f} {x2:.1f},{y2:.1f}"


# ---------------------------------------------------------------------------
# Maven Pro wordmark — vectorized to paths so the title matches the README
# banner exactly and renders identically without the font installed.
# Falls back to <text> (handled by the caller) if fontTools/the font is absent.
# ---------------------------------------------------------------------------
def maven_wordmark(text: str, x: float, baseline: float, em: float, fill: str):
    try:
        from fontTools.ttLib import TTFont
        from fontTools.pens.svgPathPen import SVGPathPen
        from fontTools.pens.transformPen import TransformPen
        for cand in ("~/Library/Fonts/MavenPro-700.ttf",
                     "~/Library/Fonts/MavenPro-600.ttf", "/tmp/MavenPro.ttf"):
            fp = os.path.expanduser(cand)
            if os.path.exists(fp):
                break
        else:
            return None
        f = TTFont(fp)
        upm = f["head"].unitsPerEm
        cmap = f.getBestCmap()
        gs = f.getGlyphSet()
        s = em / upm
        penx = x
        paths = []
        for ch in text:
            g = cmap[ord(ch)]
            sp = SVGPathPen(gs)
            gs[g].draw(TransformPen(sp, (s, 0, 0, -s, penx, baseline)))
            paths.append(sp.getCommands())
            penx += gs[g].width * s
        body = "".join(f'<path d="{d}"/>' for d in paths)
        return f'<g fill="{fill}">{body}</g>'
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Build SVG
# ---------------------------------------------------------------------------
def build_svg() -> str:
    logo_b64 = base64.b64encode((ART / "logo" / "pyfly-logo.png").read_bytes()).decode()
    logo_uri = f"data:image/png;base64,{logo_b64}"

    # Logo: native 1648 × 748
    # Place with 40 px bottom safe margin: logo_y + logo_h = H - 40
    logo_w = 540
    logo_h = int(logo_w * 748 / 1648)   # ≈ 245 px
    logo_x = (W - logo_w) // 2
    logo_y = H - 40 - logo_h             # ≈ 1815

    # -------------------------------------------------------------------------
    # EDA Network motif  (illustration zone: y = 80 … 1080)
    # Hub: center, slightly above midpoint of illustration zone
    # -------------------------------------------------------------------------
    HUB_X, HUB_Y, HUB_R = 750, 580, 72

    # Satellite nodes: (cx, cy, radius, label)
    SATS = [
        (260,  210, 44, "CMD"),
        (650,  185, 40, "EVT"),
        (1080, 240, 44, "SAGA"),
        (1220, 540, 38, "Q"),
        (1110, 870, 44, "HTTP"),
        (390,  930, 40, "DATA"),
        (150,  610, 38, "MSG"),
    ]

    # Small accent nodes (no labels, atmospheric depth)
    ACCENT_NODES = [
        (500,  100, 22),
        (930,  130, 18),
        (1350, 360, 20),
        (1370, 760, 17),
        (980, 1020, 19),
        (200,  990, 16),
        (80,   400, 18),
    ]

    # Peer-to-peer connections (satellite ring, one hop)
    CONNECTIONS_PEER = [(0, 1), (1, 2), (2, 3), (3, 4), (4, 5), (5, 6), (6, 0)]

    # SVG fragment list
    parts: list[str] = []

    # 1. Background fill
    parts.append(f'<rect width="{W}" height="{H}" fill="{INK}"/>')

    # 2. Very faint hex-grid texture over whole canvas
    parts.append('<g opacity="0.035" fill="none" stroke="#43b02a" stroke-width="0.8">')
    for row in range(15):
        for col in range(10):
            gx = col * 185 - 60
            gy = row * 165 + (80 if col % 2 else 0) - 30
            pts = hex_points(gx, gy, 78)
            parts.append(f'<polygon points="{pts}"/>')
    parts.append('</g>')

    # 3. Radial glow behind hub (defined inline; hoisted to <defs> by assembler)
    grad_id = "hubglow"
    parts.append(
        f'<radialGradient id="{grad_id}" cx="{HUB_X/W:.4f}" cy="{HUB_Y/H:.4f}" r="0.30" '
        f'fx="{HUB_X/W:.4f}" fy="{HUB_Y/H:.4f}" gradientUnits="objectBoundingBox">'
        f'<stop offset="0" stop-color="#43b02a" stop-opacity="0.20"/>'
        f'<stop offset="1" stop-color="#43b02a" stop-opacity="0"/>'
        f'</radialGradient>'
    )
    parts.append(f'<rect width="{W}" height="{H}" fill="url(#{grad_id})"/>')

    # 4. Peer connection lines (dim ring)
    parts.append(
        f'<g fill="none" stroke="{GREEN_DIM}" stroke-width="1.5" opacity="0.45">'
    )
    for (a, b) in CONNECTIONS_PEER:
        sx, sy = SATS[a][0], SATS[a][1]
        ex, ey = SATS[b][0], SATS[b][1]
        parts.append(f'<path d="{quad_path(sx, sy, ex, ey)}"/>')
    parts.append('</g>')

    # 5. Hub spokes (brighter, thicker)
    parts.append(
        f'<g fill="none" stroke="{GREEN}" stroke-width="2.5" opacity="0.55">'
    )
    for (sx, sy, _, _) in SATS:
        parts.append(f'<path d="{quad_path(HUB_X, HUB_Y, sx, sy)}"/>')
    parts.append('</g>')

    # 6. Event-pulse dots along spokes at 1/3 and 2/3
    PULSE_COLORS = [AMBER, GREEN, AMBER, GREEN, AMBER, GREEN, AMBER]
    for i, (sx, sy, _, _) in enumerate(SATS):
        col = PULSE_COLORS[i % len(PULSE_COLORS)]
        for t, r_dot, op_dot in [(0.32, 6, 0.90), (0.65, 4, 0.55)]:
            px = HUB_X + (sx - HUB_X) * t
            py = HUB_Y + (sy - HUB_Y) * t
            parts.append(
                f'<circle cx="{px:.1f}" cy="{py:.1f}" r="{r_dot}" '
                f'fill="{col}" opacity="{op_dot}"/>'
            )

    # 7. Accent nodes (tiny, atmospheric)
    for (ax, ay, ar) in ACCENT_NODES:
        parts.append(node(ax, ay, ar, DEEP, GREEN_DIM, 1.4, opacity=0.65))

    # 8. Satellite nodes with outer ring + label
    FONT_SAT = "Maven Pro,Avenir Next,Avenir,Helvetica Neue,Helvetica,Arial,sans-serif"
    for (sx, sy, sr, lbl) in SATS:
        parts.append(node(sx, sy, sr + 9, "none", GREEN, 1.2, opacity=0.28))
        parts.append(node(sx, sy, sr, GREEN_MID, GREEN, 2.2))
        fs = 23 if len(lbl) <= 3 else 19
        parts.append(
            f'<text x="{sx}" y="{sy + 1}" text-anchor="middle" '
            f'dominant-baseline="middle" fill="{LIGHT}" '
            f'font-size="{fs}" font-weight="700" '
            f'font-family="{FONT_SAT}" letter-spacing="1">{lbl}</text>'
        )

    # 9. Hub node
    parts.append(node(HUB_X, HUB_Y, HUB_R + 16, "none", GREEN, 1.8, opacity=0.40))
    parts.append(node(HUB_X, HUB_Y, HUB_R + 4,  "none", GREEN, 2.8, opacity=0.75))
    parts.append(node(HUB_X, HUB_Y, HUB_R, GREEN_MID, AMBER, 3.8))
    parts.append(
        f'<text x="{HUB_X}" y="{HUB_Y + 2}" text-anchor="middle" '
        f'dominant-baseline="middle" fill="{WHITE}" '
        f'font-size="46" font-weight="800" '
        f'font-family="{FONT_SAT}" letter-spacing="-1">F</text>'
    )

    # 10. Atmospheric micro-labels (subtle, spaced out)
    MICRO = [
        (500,   76, "EVENTS"),
        (1320, 315, "ASYNC"),
        (160,  375, "REACTIVE"),
        (960,  1050, "CQRS"),
    ]
    for (mx, my, mlbl) in MICRO:
        parts.append(
            f'<text x="{mx}" y="{my}" text-anchor="middle" fill="{GREEN}" '
            f'font-size="18" font-weight="400" opacity="0.50" '
            f'font-family="{FONT_SAT}" letter-spacing="3.5">{mlbl}</text>'
        )

    # -------------------------------------------------------------------------
    # Amber + green divider rules
    # -------------------------------------------------------------------------
    RULE_Y = 1110
    parts.append(
        f'<rect x="100" y="{RULE_Y}" width="1300" height="6" fill="{AMBER}" rx="3"/>'
    )
    parts.append(
        f'<rect x="100" y="{RULE_Y + 14}" width="1300" height="1.5" '
        f'fill="{GREEN}" opacity="0.45" rx="1"/>'
    )

    # -------------------------------------------------------------------------
    # Publisher label — top edge, small caps, generous letter-spacing
    # -------------------------------------------------------------------------
    FONT = "Maven Pro,Avenir Next,Avenir,Helvetica Neue,Helvetica,Arial,sans-serif"
    parts.append(
        f'<text x="{W // 2}" y="68" text-anchor="middle" '
        f'fill="{MUTED}" font-size="26" font-weight="500" '
        f'letter-spacing="9" font-family="{FONT}">'
        f'FIREFLY SOFTWARE FOUNDATION</text>'
    )

    # -------------------------------------------------------------------------
    # Title block
    # -------------------------------------------------------------------------
    TY = RULE_Y + 68   # baseline anchor of first title line

    # "PyFly" — Maven Pro, vectorized (matches the README banner); <text> fallback
    _wm = maven_wordmark("PyFly", 108, TY + 200, 236, WHITE)
    if _wm:
        parts.append(_wm)
    else:
        parts.append(
            f'<text x="108" y="{TY + 200}" '
            f'fill="{WHITE}" font-size="240" font-weight="800" '
            f'font-family="Maven Pro,{FONT}" letter-spacing="-7">PyFly</text>'
        )

    # Thin amber separator under PyFly (visual rhythm)
    RULE2_Y = TY + 218
    parts.append(
        f'<rect x="108" y="{RULE2_Y}" width="700" height="3" '
        f'fill="{AMBER}" opacity="0.60" rx="1.5"/>'
    )

    # "by Example" — brand green, demi-bold
    parts.append(
        f'<text x="112" y="{RULE2_Y + 98}" '
        f'fill="{GREEN}" font-size="94" font-weight="600" '
        f'font-family="{FONT}" letter-spacing="-1">by Example</text>'
    )

    # Subtitle (two lines)
    SUB_Y = RULE2_Y + 170
    for i, line in enumerate([
        "Event-Driven Python Microservices",
        "with the Firefly Framework",
    ]):
        parts.append(
            f'<text x="112" y="{SUB_Y + i * 56}" '
            f'fill="{MUTED_LT}" font-size="42" font-weight="400" '
            f'font-family="{FONT}" letter-spacing="0.5">{line}</text>'
        )

    # -------------------------------------------------------------------------
    # Logo — centered, bottom zone
    # -------------------------------------------------------------------------
    parts.append(
        f'<image x="{logo_x}" y="{logo_y}" width="{logo_w}" height="{logo_h}" '
        f'xlink:href="{logo_uri}" opacity="0.90"/>'
    )

    # -------------------------------------------------------------------------
    # Assemble: hoist gradient elements into <defs>
    # -------------------------------------------------------------------------
    defs_tags = ("<radialGradient", "<linearGradient")
    defs_parts = [p for p in parts if p.startswith(defs_tags)]
    body_parts = [p for p in parts if not p.startswith(defs_tags)]

    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'width="{W}" height="{H}" viewBox="0 0 {W} {H}">\n'
        f'<defs>{"".join(defs_parts)}</defs>\n'
        + "\n".join(body_parts)
        + "\n</svg>"
    )
    return svg


def _render_png(svg_path: Path, png_path: Path) -> None:
    """Rasterize the cover. Prefer cairosvg; fall back to resvg (npx) if absent."""
    try:
        import cairosvg
        cairosvg.svg2png(url=str(svg_path), write_to=str(png_path),
                         output_width=W, output_height=H)
        return
    except Exception:
        pass
    import subprocess
    subprocess.run(["npx", "-y", "@resvg/resvg-js-cli", str(svg_path), str(png_path)],
                   check=True)


def main() -> None:
    svg = build_svg()
    (ART / "cover.svg").write_text(svg, encoding="utf-8")
    _render_png(ART / "cover.svg", ART / "cover.png")
    print("wrote cover.svg and cover.png")


if __name__ == "__main__":
    main()
