"""Generate book/art/cover.svg (Daylight, logo embedded) and cover.png."""
from __future__ import annotations
import base64
from pathlib import Path
import cairosvg

ART = Path(__file__).resolve().parents[1] / "art"
W, H = 1500, 2100  # 5:7 cover

def build_svg() -> str:
    logo_b64 = base64.b64encode((ART / "logo" / "pyfly-logo.png").read_bytes()).decode()
    logo = f"data:image/png;base64,{logo_b64}"
    # logo native ratio 1648:748 -> place at width 1100, centered, upper area
    lw = 1100; lh = int(lw * 748 / 1648); lx = (W - lw)//2; ly = 360
    band_y = int(H * 0.64)
    return f'''<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"
  width="{W}" height="{H}" viewBox="0 0 {W} {H}">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#fcfff8"/><stop offset="1" stop-color="#e7f5d8"/></linearGradient>
    <linearGradient id="band" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#49bd2e"/><stop offset="1" stop-color="#2b881b"/></linearGradient>
  </defs>
  <rect width="{W}" height="{H}" fill="url(#bg)"/>
  <g fill="#7bc63f" opacity="0.7">
    <path d="M300 470 l13 32 32 13 -32 13 -13 32 -13 -32 -32 -13 32 -13z"/>
    <path d="M1230 380 l10 24 24 10 -24 10 -10 24 -10 -24 -24 -10 24 -10z"/>
    <path d="M1255 980 l9 22 22 9 -22 9 -9 22 -9 -22 -22 -9 22 -9z"/></g>
  <image x="{lx}" y="{ly}" width="{lw}" height="{lh}" xlink:href="{logo}"/>
  <text x="{W//2}" y="200" text-anchor="middle" fill="#2c8a1c" font-size="40"
    letter-spacing="12" font-family="Helvetica,Arial,sans-serif" font-weight="700">FIREFLY SOFTWARE FOUNDATION</text>
  <rect x="0" y="{band_y}" width="{W}" height="{H-band_y}" fill="url(#band)"/>
  <rect x="0" y="{band_y}" width="{W}" height="16" fill="#ffc24b"/>
  <text x="120" y="{band_y+220}" fill="#ffffff" font-size="170" font-weight="800"
    font-family="Helvetica,Arial,sans-serif" letter-spacing="-2">by Example</text>
  <text x="128" y="{band_y+320}" fill="#eafbe0" font-size="52"
    font-family="Helvetica,Arial,sans-serif">Event-Driven Python Microservices</text>
  <text x="128" y="{band_y+388}" fill="#eafbe0" font-size="52"
    font-family="Helvetica,Arial,sans-serif">with the Firefly Framework</text>
  <text x="128" y="{band_y+486}" fill="#bfeaa3" font-size="38" letter-spacing="6"
    font-family="Helvetica,Arial,sans-serif">A HANDS-ON, PROJECT-DRIVEN GUIDE</text>
</svg>'''

def main() -> None:
    svg = build_svg()
    (ART / "cover.svg").write_text(svg, encoding="utf-8")
    cairosvg.svg2png(bytestring=svg.encode(), write_to=str(ART / "cover.png"),
                     output_width=W, output_height=H)
    print("wrote cover.svg and cover.png")

if __name__ == "__main__":
    main()
