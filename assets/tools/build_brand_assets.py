#!/usr/bin/env python3
"""Regenerate the PyFly README brand assets — assets/banner.svg and the six
diagram SVGs — from the shared visual kit.

Self-contained: derives the repo root from its own location, generates the snake
artwork from assets/pyfly-logo.png, vectorizes the Maven Pro wordmark, and uses
the vendored Simple-Icons paths in icons.py.

Requirements: Python 3.12+, fontTools, Pillow (the book/.venv has both:
  book/.venv/bin/python assets/tools/build_brand_assets.py
). Maven Pro is fetched (and cached) from Google Fonts if not already installed.
Render/verify the output with resvg:  npx -y @resvg/resvg-js-cli <in.svg> <out.png>
"""
from __future__ import annotations
import base64, io, math, os, sys, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
ASSETS = REPO / "assets"
CACHE = HERE / ".cache"
CACHE.mkdir(exist_ok=True)
sys.path.insert(0, str(HERE))
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from PIL import Image
try:
    from icons import ICONS
except Exception:
    ICONS = {}

# --------------------------------------------------------------------------- palette
WHITE="#ffffff"; STROKE="#dfe7d4"; GREEN="#4cbb2f"; GREEN2="#43b02a"; GREEND="#3a9e23"
MID="#2c8a1c"; DARK="#1f5e16"; BODY="#33402e"; MUTED="#7c876f"; SUB="#f4f9ee"; BLUE="#5b7a9d"
INK="#243019"; AMBER="#c2722a"
MONO="ui-monospace,'SF Mono',Menlo,Consolas,monospace"
SANS="-apple-system,'Segoe UI',Helvetica,Arial,sans-serif"

# --------------------------------------------------------------------------- fonts
def maven_var() -> str:
    """Path to a Maven Pro variable font; fetch+cache from Google Fonts if needed."""
    for p in ("~/Library/Fonts/MavenPro[wght].ttf",):
        q = os.path.expanduser(p)
        if os.path.exists(q):
            return q
    dst = CACHE / "MavenPro.ttf"
    if not dst.exists():
        url = "https://raw.githubusercontent.com/google/fonts/main/ofl/mavenpro/MavenPro%5Bwght%5D.ttf"
        with urllib.request.urlopen(url, timeout=30) as r:  # verified TLS (default context)
            dst.write_bytes(r.read())
    return str(dst)

_VAR = maven_var()
def load_maven(wght: int) -> TTFont:
    f = TTFont(_VAR); instantiateVariableFont(f, {"wght": wght}, inplace=True); return f

def word_paths(f: TTFont, text: str, em: float, x0: float, baseline: float, tracking=0.0):
    upm=f["head"].unitsPerEm; cmap=f.getBestCmap(); gs=f.getGlyphSet()
    s=em/upm; penx=x0; out=[]
    for ch in text:
        g=cmap[ord(ch)]; sp=SVGPathPen(gs); gs[g].draw(TransformPen(sp,(s,0,0,-s,penx,baseline)))
        out.append(sp.getCommands()); penx+=gs[g].width*s+tracking
    return out, penx-x0

# Arial metrics ≈ system sans, for guaranteed-fit box sizing
_AR={}
def _arial(bold):
    k="b" if bold else "r"
    if k not in _AR:
        p=f"/System/Library/Fonts/Supplemental/Arial{' Bold' if bold else ''}.ttf"
        try: _AR[k]=(lambda f:(f["head"].unitsPerEm,f.getBestCmap(),f.getGlyphSet()))(TTFont(p))
        except Exception: _AR[k]=None
    return _AR[k]
def tw(s,size,bold=False):
    m=_arial(bold)
    if not m: return 0.56*size*len(str(s))
    upm,cmap,gs=m; w=0
    for ch in str(s): g=cmap.get(ord(ch)); w+= gs[g].width if g else upm*0.5
    return w/upm*size
def mw(s,size): return 0.602*size*len(str(s))
def esc(s): return str(s).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

# --------------------------------------------------------------------------- snake art
def gen_snake():
    logo=Image.open(ASSETS/"pyfly-logo.png").convert("RGBA")
    crop=logo.crop((0,90,535,650)); crop=crop.crop(crop.getbbox())
    def uri(h, colors):
        im=crop.resize((round(crop.width*h/crop.height),h), Image.LANCZOS)
        buf=io.BytesIO(); im.save(buf,format="PNG",optimize=True); data=buf.getvalue()
        if len(data)>150_000 or colors:
            q=im.quantize(colors=colors or 128, method=Image.FASTOCTREE)
            buf=io.BytesIO(); q.save(buf,format="PNG",optimize=True); data=buf.getvalue()
        return "data:image/png;base64,"+base64.b64encode(data).decode(), im.width/im.height
    hero,aspect=uri(520,None); mark,_=uri(132,64)
    return hero,aspect,mark

HERO_URI, HERO_ASPECT, SNAKE_MARK = gen_snake()

# --------------------------------------------------------------------------- kit
def defs(cx,cy,r=520):
    return f'''<defs>
    <linearGradient id="hdr" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{GREEN}"/><stop offset="1" stop-color="{GREEND}"/></linearGradient>
    <linearGradient id="door" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#5cc739"/><stop offset="1" stop-color="#2f8f1c"/></linearGradient>
    <linearGradient id="bed" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#1c3217"/><stop offset="1" stop-color="#12220e"/></linearGradient>
    <radialGradient id="amb" cx="{cx}" cy="{cy}" r="{r}" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="{GREEN}" stop-opacity="0.10"/><stop offset="1" stop-color="{GREEN}" stop-opacity="0"/></radialGradient>
    <filter id="sh" x="-25%" y="-25%" width="150%" height="170%"><feDropShadow dx="0" dy="2" stdDeviation="3.2" flood-color="#1f5e16" flood-opacity="0.15"/></filter>
    <marker id="arr" markerWidth="9" markerHeight="9" refX="6.2" refY="3.2" orient="auto"><path d="M0 0L7 3.2L0 6.4Z" fill="{GREEND}"/></marker>
    <marker id="arrb" markerWidth="9" markerHeight="9" refX="6.2" refY="3.2" orient="auto"><path d="M0 0L7 3.2L0 6.4Z" fill="{BLUE}"/></marker>
    <marker id="arra" markerWidth="9" markerHeight="9" refX="6.2" refY="3.2" orient="auto"><path d="M0 0L7 3.2L0 6.4Z" fill="{AMBER}"/></marker>
    <g id="fly"><circle r="8.5" fill="#9bd24a" opacity="0.10"/><circle r="4.6" fill="#c2e85f" opacity="0.22"/><circle r="2.4" fill="#dff58a" opacity="0.75"/><circle r="1.2" fill="#f2ffd0"/></g>
  </defs>'''
def frame(w,h):
    return (f'<rect width="{w}" height="{h}" fill="{WHITE}"/>'
            f'<rect x="3" y="3" width="{w-6}" height="{h-6}" rx="18" fill="{WHITE}" stroke="{STROKE}" stroke-width="1.5"/>'
            f'<rect x="3" y="3" width="{w-6}" height="{h-6}" rx="18" fill="url(#amb)"/>')
def mote(x,y,s=1.0): return f'<use href="#fly" transform="translate({x},{y}) scale({s})"/>'
def badge(x,y,n,r=10.5):
    return (f'<circle cx="{x}" cy="{y}" r="{r}" fill="{DARK}"/>'
            f'<text x="{x}" y="{y+3.6}" text-anchor="middle" fill="#fff" font-size="{r}" font-weight="700" font-family="{SANS}">{n}</text>')
def icon(name,cx,cy,size,color=None):
    ic=ICONS.get(name)
    if not ic: return ""
    vb=[float(v) for v in ic["vb"].split()]; vw,vh=vb[2],vb[3]; s=size/max(vw,vh)
    return (f'<g transform="translate({cx:.1f},{cy:.1f}) scale({s:.4f}) translate({-vw/2:.1f},{-vh/2:.1f})">'
            f'<path d="{ic["d"]}" fill="{color or ic["color"]}"/></g>')
def snake(x,y,h):
    if not SNAKE_MARK: return mote(x+12,y+h/2,1.2)
    return f'<image x="{x:.1f}" y="{y:.1f}" width="{h*0.91:.1f}" height="{h:.1f}" href="{SNAKE_MARK}"/>'
def title(w,t,sub=None,repo="fireflyframework-pyfly"):
    s=[snake(24,18,38),
       f'<text x="64" y="45" font-size="21" font-weight="800" fill="{INK}" font-family="{SANS}" letter-spacing="0.2">{esc(t)}</text>',
       f'<text x="{w-26}" y="42" text-anchor="end" font-size="12" font-weight="600" fill="#9cbf86" font-family="{MONO}">{repo}</text>',
       f'<line x1="26" y1="62" x2="{w-26}" y2="62" stroke="{GREEN2}" stroke-width="1.4" opacity="0.42"/>']
    if sub: s.append(f'<text x="26" y="84" font-size="12.5" font-style="italic" fill="{MUTED}" font-family="{SANS}">{esc(sub)}</text>')
    return "\n  ".join(s)
def svgdoc(w,h,label,body,amb=None):
    cx,cy=amb or (w-60,40)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
            f'role="img" aria-label="{esc(label)}" font-family="{SANS}">\n  '+defs(cx,cy)+"\n  "+frame(w,h)+"\n  "+body+"\n</svg>\n")

WARN=[]
def need(header,lines,mono=True,icon=False,pad=30):
    hw=tw(header,11,True)+(22 if icon else 0)
    lw=max([(mw(l,10) if mono else tw(l,10)) for l in lines]+[0]); return max(hw,lw)+pad
def fbox(x,y,w,h,header,lines,mono=True,hdrfill="url(#hdr)",stroke=GREEN2,hc="#fff",icon_name=None,rects=None):
    fam=MONO if mono else SANS
    s=[f'<g filter="url(#sh)"><rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h}" rx="10" fill="{WHITE}" stroke="{stroke}" stroke-width="1.8"/>'
       f'<path d="M{x:.1f} {y+10}a10 10 0 0 1 10 -10h{w-20:.1f}a10 10 0 0 1 10 10v13H{x:.1f}Z" fill="{hdrfill}"/></g>',
       f'<text x="{x+12:.1f}" y="{y+15}" font-size="11" font-weight="700" fill="{hc}" font-family="{SANS}">{esc(header)}</text>']
    if icon_name: s.append(icon(icon_name,x+w-16,y+11,15,"#ffffff"))
    for i,ln in enumerate(lines):
        s.append(f'<text x="{x+12:.1f}" y="{y+39+i*15}" font-size="10" fill="{BODY}" font-family="{fam}">{esc(ln)}</text>')
    if rects is not None: rects.append((x,y,x+w,y+h))
    return "".join(s)
def edge(cx,cy,w,h,fx,fy):
    dx,dy=fx-cx,fy-cy
    if dx==0 and dy==0: return cx,cy
    s=min((w/2)/abs(dx) if dx else 9e9,(h/2)/abs(dy) if dy else 9e9); return cx+dx*s,cy+dy*s
def arrow(x1,y1,x2,y2,color=GREEND,dash=None,mk="arr",sw=1.8):
    d=f' stroke-dasharray="{dash}"' if dash else ""
    return f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{color}" stroke-width="{sw}"{d} marker-end="url(#{mk})"/>'
def spark(cx,cy,r,color):
    return (f'<path d="M{cx} {cy-r}L{cx+r*0.28} {cy-r*0.28}L{cx+r} {cy}L{cx+r*0.28} {cy+r*0.28}'
            f'L{cx} {cy+r}L{cx-r*0.28} {cy+r*0.28}L{cx-r} {cy}L{cx-r*0.28} {cy-r*0.28}Z" fill="{color}"/>')
def check(name,rects,pad=2):
    for i in range(len(rects)):
        for j in range(i+1,len(rects)):
            a,b=rects[i],rects[j]
            if a[0]<b[2]-pad and b[0]<a[2]-pad and a[1]<b[3]-pad and b[1]<a[3]-pad:
                WARN.append(f"{name}: overlap {i}&{j}")

# --------------------------------------------------------------------------- banner
def build_banner():
    W,H=1280,320
    fH,fM,fR=load_maven(800),load_maven(600),load_maven(500)
    cap=fH["OS/2"].sCapHeight/fH["head"].unitsPerEm
    EM=150; sh=252; sw=sh*HERO_ASPECT; sx,sy=22,(H-sh)/2
    wm_x=sx+sw+46; wm_base=176
    wm,wmw=word_paths(fH,"pyFly",EM,wm_x,wm_base,tracking=-2); cappx=cap*EM; wmt=wm_base-cappx
    tl_x=wm_x+4
    t1,t1w=word_paths(fM,"Event-Driven Python Microservices",23,tl_x,250)
    t2,_=word_paths(fR,"async-native  ·  hexagonal  ·  Spring-Boot DX for Python",15.5,tl_x,282)
    def fly(x,y,s,k): return f'<use href="#fly{k}" transform="translate({x},{y}) scale({s})"/>'
    bg=[(706,64,1.3,.5),(792,118,1.0,.42),(905,58,1.5,.5),(1002,104,1.1,.42),(1108,72,1.4,.48),(1182,128,1.0,.36),
        (835,214,1.3,.44),(968,250,1.05,.38),(1092,232,1.5,.5),(1204,206,1.0,.34),(742,176,1.2,.4),(1024,182,.9,.3),(894,150,1.1,.4),(1150,168,1.0,.4)]
    hero=[(980,116,1.9,"g"),(1066,168,1.5,"a"),(900,196,1.15,"g"),(1150,104,1.05,"a"),(1108,220,.95,"g"),(828,128,1.0,"a")]
    def grp(paths,fill): return f'<g fill="{fill}">'+"".join(f'<path d="{d}"/>' for d in paths)+'</g>'
    svg=f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" aria-label="PyFly — Event-Driven Python Microservices with the Firefly Framework">
  <defs>
    <linearGradient id="sky" x1="0" y1="0" x2="{W}" y2="{H}" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#07110b"/><stop offset="0.5" stop-color="#0a1410"/><stop offset="1" stop-color="#0c1a0f"/></linearGradient>
    <radialGradient id="ambient" cx="985" cy="92" r="470" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#4cbb2f" stop-opacity="0.22"/><stop offset="0.5" stop-color="#7ed321" stop-opacity="0.07"/><stop offset="1" stop-color="#7ed321" stop-opacity="0"/></radialGradient>
    <radialGradient id="snakeGlow" cx="50%" cy="48%" r="60%"><stop offset="0" stop-color="#7ed321" stop-opacity="0.30"/><stop offset="0.6" stop-color="#4cbb2f" stop-opacity="0.08"/><stop offset="1" stop-color="#4cbb2f" stop-opacity="0"/></radialGradient>
    <linearGradient id="wm" x1="0" y1="{wmt}" x2="0" y2="{wm_base}" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#b6f06a"/><stop offset="0.5" stop-color="#7ed321"/><stop offset="1" stop-color="#46b522"/></linearGradient>
    <g id="flyg"><circle r="13" fill="#6abf2e" opacity="0.09"/><circle r="7.5" fill="#9bd24a" opacity="0.18"/><circle r="3.6" fill="#cdf06a" opacity="0.6"/><circle r="1.9" fill="#f2ffd0"/></g>
    <g id="flya"><circle r="13" fill="#9bd24a" opacity="0.08"/><circle r="7.5" fill="#c2e85f" opacity="0.16"/><circle r="3.6" fill="#dff58a" opacity="0.6"/><circle r="1.9" fill="#fbffe2"/></g>
  </defs>
  <rect width="{W}" height="{H}" fill="url(#sky)"/>
  <rect width="{W}" height="{H}" fill="url(#ambient)"/>
  <g fill="#bfe27a">{"".join(f'<circle cx="{x}" cy="{y}" r="{r}" opacity="{o}"/>' for x,y,r,o in bg)}</g>
  <g fill="none" stroke="#5cc12e" stroke-linecap="round">
    <path d="M860,250 C920,196 962,168 980,116" stroke-width="2.2" opacity="0.10"/>
    <path d="M860,250 C920,196 962,168 980,116" stroke-width="0.9" opacity="0.22"/>
    <path d="M1190,242 C1150,206 1120,190 1066,168" stroke-width="1.8" opacity="0.08"/>
  </g>
  {"".join(fly(x,y,s,k) for x,y,s,k in hero)}
  <ellipse cx="{sx+sw/2:.0f}" cy="{H/2:.0f}" rx="{sw*0.62:.0f}" ry="148" fill="url(#snakeGlow)"/>
  <image x="{sx:.1f}" y="{sy:.1f}" width="{sw:.1f}" height="{sh}" href="{HERO_URI}"/>
  <g transform="skewX(-7)" fill="url(#wm)" stroke="#16500f" stroke-width="5.5" stroke-linejoin="round" paint-order="stroke">{"".join(f'<path d="{d}"/>' for d in wm)}</g>
  {fly(wm_x+wmw+22, wmt+10, 1.25, "a")}
  <rect x="{tl_x}" y="214" width="{max(t1w,260):.0f}" height="2.4" rx="1.2" fill="#43b02a" opacity="0.55"/>
  {grp(t1,"#e3f4d4")}
  {grp(t2,"#8fb673")}
  <text x="{W-26}" y="34" text-anchor="end" font-family="{MONO}" font-size="12" fill="#5f8050" opacity="0.9" letter-spacing="0.4">fireflyframework-pyfly</text>
</svg>'''
    (ASSETS/"banner.svg").write_text(svg)

# --------------------------------------------------------------------------- diagrams
def architecture():
    W,H=864,612; X,WD=66,726
    layers=[("1","Cross-Cutting","6","aop · observability · actuator · admin · testing · cli"),
            ("2","Integration","6","idp · ecm · notifications · callbacks · webhooks · starters"),
            ("3","Infrastructure","14","security · messaging · eda · cache · client · scheduling · resilience · transactional · …"),
            ("4","Application","7","web · server · data · data-relational · data-document · cqrs · validation"),
            ("5","Foundation","6","kernel · core · container · context · config · logging")]
    b=[title(W,"Architecture at a glance","One install, one decorator — five cohesive layers on an async-native core.")]
    fy=100
    b.append(f'<rect x="{X}" y="{fy}" width="{WD}" height="50" rx="12" fill="url(#door)" stroke="#2b8a1a" stroke-width="1.2" filter="url(#sh)"/>')
    b.append(f'<rect x="{X+14}" y="{fy+6}" width="{WD-28}" height="2" rx="1" fill="#fffdf5" opacity="0.28"/>')
    b.append(icon("python",X+28,fy+25,22,"#0f3d08"))
    b.append(f'<text x="{X+50}" y="{fy+20}" font-size="10.5" font-weight="800" fill="#0f3d08" letter-spacing="1.4">THE FRONT DOOR</text>')
    b.append(f'<text x="{X+50}" y="{fy+39}" font-size="15" font-weight="800" fill="#0c3206" font-family="{MONO}">pyfly + extras · @pyfly_application</text>')
    b.append(f'<text x="{X+WD-16}" y="{fy+20}" text-anchor="end" font-size="11" fill="#0f3d08" font-weight="700">one install</text>')
    b.append(f'<text x="{X+WD-16}" y="{fy+39}" text-anchor="end" font-size="11" fill="#0f3d08" font-weight="700">one decorator</text>')
    b.append(f'<g stroke="{GREEN2}" stroke-width="1.4" stroke-dasharray="2 3" opacity="0.7">'+"".join(f'<line x1="{X+WD*f:.0f}" y1="{fy+50}" x2="{X+WD*f:.0f}" y2="170"/>' for f in (.12,.37,.62,.87))+'</g>')
    b.append(f'<line x1="46" y1="174" x2="46" y2="516" stroke="{GREEND}" stroke-width="2.2" marker-end="url(#arr)"/>')
    b.append(f'<text x="28" y="345" text-anchor="middle" font-size="10.5" font-weight="700" fill="{MID}" letter-spacing="0.08em" transform="rotate(-90,28,345)">DEPENDS ON</text>')
    by=170; BH=66; GAP=5.5
    for i,(n,name,cnt,mods) in enumerate(layers):
        y=by+i*(BH+GAP)
        b.append(f'<g filter="url(#sh)"><rect x="{X}" y="{y:.1f}" width="{WD}" height="{BH}" rx="11" fill="{WHITE}" stroke="{GREEN2}" stroke-width="2"/>'
                 f'<path d="M{X} {y+11:.1f}a11 11 0 0 1 11 -11h{WD-22}a11 11 0 0 1 11 11v15H{X}Z" fill="url(#hdr)"/></g>')
        b.append(badge(X+20,y+13,n))
        b.append(f'<text x="{X+38}" y="{y+18:.1f}" fill="#fff" font-size="13" font-weight="700">{name}</text>')
        b.append(f'<text x="{X+44+tw(name,13,True):.0f}" y="{y+18:.1f}" fill="#e6f7da" font-size="10" font-weight="600">({cnt} modules)</text>')
        if i==4: b.append(f'<text x="{X+WD-14}" y="{y+18:.1f}" text-anchor="end" fill="#dff3d4" font-size="9.5" font-weight="700" letter-spacing="0.06em">BASE LAYER</text>')
        b.append(f'<text x="{X+22}" y="{y+49:.1f}" fill="{BODY}" font-size="11" font-family="{MONO}">{esc(mods)}</text>')
    yb=by+5*(BH+GAP)+4
    b.append(f'<rect x="{X}" y="{yb:.1f}" width="{WD}" height="46" rx="12" fill="url(#bed)"/>')
    b.append(f'<circle cx="{X+30}" cy="{yb+23:.1f}" r="12" fill="none" stroke="#9bd24a" stroke-width="1.4" opacity="0.7"/><ellipse cx="{X+30}" cy="{yb+23:.1f}" rx="12" ry="4.5" fill="none" stroke="#c2e85f" stroke-width="1" opacity="0.5"/><circle cx="{X+30}" cy="{yb+23:.1f}" r="3.3" fill="#dff58a"/>')
    b.append(f'<text x="{X+56}" y="{yb+20:.1f}" font-size="13" font-weight="800" fill="#eaffc9" font-family="{MONO}">async core</text>')
    b.append(f'<text x="{X+56}" y="{yb+36:.1f}" font-size="10.5" fill="#bcd9a0">asyncio · uvloop · ASGI — the event loop every layer runs on</text>')
    b.append(f'<text x="{X+WD-14}" y="{yb+27:.1f}" text-anchor="end" font-size="10.5" fill="#8fb573" font-weight="600">Granian · Uvicorn · Hypercorn</text>')
    (ASSETS/"architecture.svg").write_text(svgdoc(W,H,"PyFly architecture: one front door over five layers on an async core.","\n  ".join(b),amb=(720,72)))

def hexagonal():
    W,H=924,694; cx,cy=462,376; Rhex=104; R=[]
    b=[title(W,"Hexagonal architecture — ports and adapters","Your code depends on Protocol ports; the DI container wires real adapters at startup.")]
    items=[("WebServerPort","Starlette / FastAPI","fastapi"),("HttpClientPort","httpx · SOAP · gRPC",None),
           ("CacheAdapter","Redis / in-memory","redis"),("RepositoryPort","SQLAlchemy / MongoDB","postgresql"),
           ("EventPublisher","in-memory / outbox",None),("MessageBrokerPort","Kafka / RabbitMQ","apachekafka")]
    angles=[-90,-30,30,90,150,210]; prx,pry=214,152; arx,ary=346,232
    P=[]; A=[]
    for (port,adap,ic),a in zip(items,angles):
        ar=math.radians(a)
        px,py=cx+prx*math.cos(ar),cy+pry*math.sin(ar); ax,ay=cx+arx*math.cos(ar),cy+ary*math.sin(ar)
        pw=max(tw(port,11,True)+30,120); aw=max(tw(adap,10)+(34 if ic else 18),130)
        P.append((port,px,py,pw,34)); A.append((adap,ax,ay,aw,32,ic))
    for (port,px,py,pw,ph),(adap,ax,ay,aw,ah,ic) in zip(P,A):
        ex,ey=edge(cx,cy,Rhex*1.7,Rhex*1.5,px,py); pex,pey=edge(px,py,pw,ph,cx,cy)
        b.append(arrow(ex,ey,pex,pey,GREEND,sw=1.8))
        aex,aey=edge(ax,ay,aw,ah,px,py); pax,pay=edge(px,py,pw,ph,ax,ay)
        b.append(arrow(aex,aey,pax,pay,BLUE,dash="5 3",mk="arrb",sw=1.5))
    pts=" ".join(f"{cx+Rhex*math.cos(math.radians(a)):.1f},{cy+Rhex*math.sin(math.radians(a)):.1f}" for a in range(-90,270,60))
    b.append(f'<polygon points="{pts}" fill="{SUB}" stroke="{GREEN2}" stroke-width="2.5" filter="url(#sh)"/>')
    b.append(f'<text x="{cx}" y="{cy-26}" text-anchor="middle" font-size="13" font-weight="800" fill="{MID}" letter-spacing="0.5">APPLICATION CORE</text>')
    b.append(f'<text x="{cx}" y="{cy-7}" text-anchor="middle" font-size="10.5" fill="{BODY}" font-family="{MONO}">@service</text>')
    b.append(f'<text x="{cx}" y="{cy+9}" text-anchor="middle" font-size="10.5" fill="{BODY}" font-family="{MONO}">@rest_controller</text>')
    b.append(f'<text x="{cx}" y="{cy+30}" text-anchor="middle" font-size="9.5" font-style="italic" fill="{MUTED}">depends on ports only</text>')
    for port,px,py,pw,ph in P:
        b.append(f'<g filter="url(#sh)"><rect x="{px-pw/2:.1f}" y="{py-ph/2:.1f}" width="{pw:.1f}" height="{ph}" rx="9" fill="{WHITE}" stroke="{GREEN2}" stroke-width="2"/></g>')
        b.append(f'<text x="{px:.1f}" y="{py+4:.1f}" text-anchor="middle" font-size="11" font-weight="700" fill="{MID}" font-family="{MONO}">{port}</text>')
        R.append((px-pw/2,py-ph/2,px+pw/2,py+ph/2))
    for adap,ax,ay,aw,ah,ic in A:
        b.append(f'<rect x="{ax-aw/2:.1f}" y="{ay-ah/2:.1f}" width="{aw:.1f}" height="{ah}" rx="8" fill="{SUB}" stroke="{GREEN2}" stroke-width="1.3" stroke-dasharray="4 3"/>')
        if ic: b.append(icon(ic,ax-aw/2+16,ay,16))
        b.append(f'<text x="{(ax+ (14 if ic else 0)):.1f}" y="{ay+4:.1f}" text-anchor="middle" font-size="10" fill="{BODY}" font-family="{MONO}">{adap}</text>')
        R.append((ax-aw/2,ay-ah/2,ax+aw/2,ay+ah/2))
    ly=H-26
    b.append(arrow(300,ly,330,ly,GREEND,sw=1.8)+f'<text x="338" y="{ly+4}" font-size="11" fill="{BODY}">core depends on port</text>')
    b.append(arrow(508,ly,538,ly,BLUE,dash="5 3",mk="arrb")+f'<text x="546" y="{ly+4}" font-size="11" fill="{BODY}">adapter implements port</text>')
    check("hexagonal",R)
    (ASSETS/"hexagonal.svg").write_text(svgdoc(W,H,"PyFly hexagonal architecture: an application core surrounded by Protocol ports with swappable adapters.","\n  ".join(b),amb=(460,40)))

def autoconf():
    W,H=908,486; R=[]
    b=[title(W,"Auto-configuration — detect, decide, bind","Each subsystem ships its own conditional config; installed libraries wire the right adapter.")]
    b.append(fbox(40,108,250,116,"DISCOVERY",['entry_points(group=','  "pyfly.auto_','   configuration")','→ each @auto_config'],rects=R))
    b.append(arrow(292,166,326,166))
    b.append(fbox(330,108,250,116,"CONDITIONS (guards)",['@conditional_on_class','  "redis.asyncio"','@conditional_on_','   missing_bean(...)'],rects=R))
    ox,ow=626,242
    for yy,(hh,ln,fill,strk,mk) in zip((108,166,224),[("✓ BIND","RedisCacheAdapter","url(#hdr)",GREEN2,"arr"),("↩ FALLBACK","InMemoryCache","url(#door)",GREEN2,"arra"),("⊘ SKIP","your @bean already wins","#7c876f",MUTED,"arr")]):
        b.append(arrow(582,166,ox-4,yy+22,GREEND if mk=="arr" else AMBER,mk=mk))
        b.append(fbox(ox,yy,ow,44,hh,[ln],hdrfill=fill,stroke=strk,rects=R))
    ey=298; ex,ew=40,828
    b.append(f'<rect x="{ex}" y="{ey}" width="{ew}" height="128" rx="12" fill="{SUB}" stroke="{STROKE}"/>')
    b.append(f'<text x="{ex+18}" y="{ey+24}" font-size="11" font-weight="700" fill="{MID}">Same pattern, every subsystem — &#8220;default with override&#8221;:</text>')
    cols=["SUBSYSTEM","DETECTS","BINDS","FALLBACK"]; cx=[ex+18,ex+200,ex+430,ex+650]
    for c,xx in zip(cols,cx): b.append(f'<text x="{xx}" y="{ey+48}" font-size="9.5" font-weight="700" fill="{MUTED}" font-family="{MONO}" letter-spacing="0.04em">{c}</text>')
    rows=[("cache","redis.asyncio","RedisCacheAdapter","InMemoryCache","redis"),
          ("messaging","aiokafka / aio-pika","KafkaAdapter","InMemoryBroker","apachekafka"),
          ("web","fastapi","FastAPIWebAdapter","Starlette","fastapi")]
    for r,(sub,det,bind,fb,ic) in enumerate(rows):
        yy=ey+70+r*17; b.append(icon(ic,ex+24,yy-3,13))
        for v,xx in zip((sub,det,bind,fb),cx):
            b.append(f'<text x="{xx+(14 if xx==cx[0] else 0)}" y="{yy}" font-size="10" fill="{BODY}" font-family="{MONO}">{v}</text>')
    b.append(f'<text x="{ex+18}" y="{ey+124}" font-size="9.5" font-style="italic" fill="{MUTED}">Third-party packages register their own via the same entry-point group — no central engine.</text>')
    check("auto-configuration",R)
    (ASSETS/"auto-configuration.svg").write_text(svgdoc(W,H,"PyFly auto-configuration: entry-point discovery, conditional guards, then bind/fallback/skip.","\n  ".join(b),amb=(740,64)))

def lifecycle():
    W,H=980,392; R=[]
    b=[title(W,"The request lifecycle","One HTTP call wired end-to-end by the DI container — you write zero plumbing.")]
    stages=[("HTTP","GET /wallets/42",None),("WebFilters","correlation · metrics",None),
            ("@rest_controller","Valid[Body[T]]","fastapi"),("@service","place_order()",None),
            ("RepositoryPort","Protocol · save()",None),("SQLAlchemy","adapter",None),("PostgreSQL","database","postgresql")]
    widths=[max(need(h,[l],mono=True,icon=bool(ic)),104) for h,l,ic in stages]
    gap=12; total=sum(widths)+gap*(len(stages)-1); x=(W-total)/2; y=150; bh=74; centers=[]
    for (h,l,ic),w in zip(stages,widths):
        b.append(fbox(x,y,w,bh,h,[l],icon_name=ic,rects=R)); centers.append(x+w/2); x+=w
        if (h,l,ic)!=stages[-1]: b.append(arrow(x,y+bh/2,x+gap,y+bh/2)); x+=gap
    x0,x1=centers[0],centers[-1]; sc=centers[3]
    b.append(arrow(sc,y,sc,y-26,MID,dash="4 3",sw=1.5))
    ebw=tw("EventPublisher → domain event",10)+24
    b.append(f'<rect x="{sc-ebw/2:.1f}" y="{y-52}" width="{ebw:.1f}" height="26" rx="8" fill="{SUB}" stroke="{GREEN2}" stroke-dasharray="4 3"/>')
    b.append(f'<text x="{sc:.1f}" y="{y-35}" text-anchor="middle" font-size="10" fill="{BODY}" font-family="{MONO}">EventPublisher → domain event</text>')
    ry=y+bh+40
    b.append(arrow(x1,ry,x0,ry,BLUE,mk="arrb",dash="6 4",sw=1.6))
    b.append(f'<text x="{(x0+x1)/2:.0f}" y="{ry-9}" text-anchor="middle" font-size="11" fill="{BLUE}" font-weight="600">response · JSON · correlation-id echoed back</text>')
    check("request-lifecycle",R)
    (ASSETS/"request-lifecycle.svg").write_text(svgdoc(W,H,"PyFly request lifecycle: HTTP through filters, controller, service, port, adapter, database; event published, JSON returned.","\n  ".join(b),amb=(820,60)))

def patterns():
    W,H=908,500; R=[]
    b=[title(W,"Distributed transactions — saga, workflow and TCC","Coordinate work across services; compensate, wait, or two-phase-commit — all first-class.")]
    steps=[("reserve","inventory"),("charge","payment"),("ship","shipping")]; nx=[182,454,726]; sy=128; nw=150; nh=54
    for i,(t,sub) in enumerate(steps):
        b.append(fbox(nx[i]-nw/2,sy,nw,nh,f"step · {t}",[sub],rects=R))
        if i<2: b.append(arrow(nx[i]+nw/2,sy+nh/2,nx[i+1]-nw/2-6,sy+nh/2))
    b.append(f'<text x="{W-40}" y="116" text-anchor="end" font-size="10" fill="{MUTED}" font-style="italic">parallel-by-default DAG</text>')
    cy2=sy+104
    for i in range(2):
        b.append(fbox(nx[i]-nw/2,cy2,nw,44,f"compensate · {('release','refund')[i]}",[("↩ inventory","↩ payment")[i]],hdrfill=AMBER,stroke=AMBER,rects=R))
        b.append(arrow(nx[i],sy+nh,nx[i],cy2-2,AMBER,dash="5 3",mk="arra",sw=1.5))
    b.append(f'<path d="M{nx[2]} {sy+nh} C {nx[2]} {cy2+10}, {nx[1]+nw/2+30} {cy2+22}, {nx[1]+nw/2+4} {cy2+22}" fill="none" stroke="{AMBER}" stroke-width="1.5" stroke-dasharray="5 3" marker-end="url(#arra)" opacity="0.8"/>')
    b.append(f'<text x="{(nx[0]+nx[2])/2:.0f}" y="{cy2+72}" text-anchor="middle" font-size="10.5" fill="#a85d22" font-weight="600">✗ any step fails → compensations run in reverse order</text>')
    py=cy2+96; pw=276; ph=116; gap=20; px0=(W-(3*pw+2*gap))/2
    cards=[("SAGA","compensation-based",["@saga / @saga_step","DAG · retries+jitter · DLQ","RecoveryService · REST API"]),
           ("WORKFLOW","durable orchestration",["@workflow_step","@wait_for_signal / _timer","child workflows · queries"]),
           ("TCC","try / confirm / cancel",["@tcc_participant","3-phase · strong consistency","FromTry() result wiring"])]
    for i,(t,sub,ls) in enumerate(cards): b.append(fbox(px0+i*(pw+gap),py,pw,ph,f"{t} · {sub}",ls))
    check("distributed-patterns",R)
    (ASSETS/"distributed-patterns.svg").write_text(svgdoc(W,H,"PyFly distributed transactions: a saga DAG with reverse-order compensation, plus Saga, Workflow and TCC summary cards.","\n  ".join(b),amb=(170,72)))

def ecosystem():
    W,H=1000,650; cx,cy=500,348; R=[]
    b=[title(W,"One framework, every runtime — the Firefly family","PyFly is the Python member of a polyglot platform that shares one programming model.")]
    members=[("Java / Spring Boot","40+ modules · Production","springboot",False),(".NET 9","CalVer 26.05+ · Beta","dotnet",False),
             ("PyFly","Python · 39 modules","__snake__",True),("Rust","tokio + axum · Active","rust",False),
             ("Go","CLI · Active","go",False),("Frontend","Angular · flyfront","angular",False),("GenAI","agents · Active","__spark__",False)]
    n=len(members); rx,ry=372,232; angles=[-90+i*360/n for i in range(n)]; nodes=[]
    for (name,meta,ic,me),a in zip(members,angles):
        ar=math.radians(a); x=cx+rx*math.cos(ar); y=cy+ry*math.sin(ar)
        w=max(tw(name,12.5,True)+(34 if ic else 14),tw(meta,9)+(34 if ic else 14))+22; h=56 if not me else 60
        nodes.append([name,meta,ic,me,x,y,w,h])
    for name,meta,ic,me,x,y,w,h in nodes:
        b.append(f'<path d="M{cx+(x-cx)*0.16:.0f} {cy+(y-cy)*0.16:.0f} Q {(cx+x)/2:.0f} {(cy+y)/2-18:.0f} {x:.0f} {y:.0f}" fill="none" stroke="{GREEN2}" stroke-width="1.2" stroke-dasharray="3 4" opacity="0.45"/>')
    b.append(f'<circle cx="{cx}" cy="{cy}" r="96" fill="url(#amb)"/>')
    b.append(f'<circle cx="{cx}" cy="{cy}" r="62" fill="{SUB}" stroke="{GREEN2}" stroke-width="2"/>')
    b.append(f'<text x="{cx}" y="{cy-6}" text-anchor="middle" font-size="13" font-weight="800" fill="{MID}">Firefly</text>')
    b.append(f'<text x="{cx}" y="{cy+11}" text-anchor="middle" font-size="13" font-weight="800" fill="{MID}">Framework</text>')
    b.append(f'<text x="{cx}" y="{cy+30}" text-anchor="middle" font-size="9" font-style="italic" fill="{MUTED}">one model · many runtimes</text>')
    for name,meta,ic,me,x,y,w,h in nodes:
        fill="url(#hdr)" if me else WHITE; tcol="#fff" if me else INK; scol="#dff3d4" if me else MUTED
        b.append(f'<g filter="url(#sh)"><rect x="{x-w/2:.1f}" y="{y-h/2:.1f}" width="{w:.1f}" height="{h}" rx="13" fill="{fill}" stroke="{GREEN2}" stroke-width="{2.6 if me else 1.6}"/></g>')
        ix=x-w/2+22
        if ic=="__snake__": b.append(snake(x-w/2+8,y-15,30))
        elif ic=="__spark__": b.append(spark(ix,y,11,"#e0a528"))
        elif ic: b.append(icon(ic,ix,y,22))
        txt=x-w/2+(40 if ic else 16)
        b.append(f'<text x="{txt:.1f}" y="{y-3:.1f}" font-size="12.5" font-weight="800" fill="{tcol}">{name}</text>')
        b.append(f'<text x="{txt:.1f}" y="{y+13:.1f}" font-size="9" fill="{scol}" font-family="{MONO}">{meta}</text>')
        if me: b.append(f'<text x="{x:.1f}" y="{y+h/2+15:.1f}" text-anchor="middle" font-size="9.5" font-weight="700" fill="{MID}">★ you are here</text>')
        R.append((x-w/2,y-h/2,x+w/2,y+h/2))
    check("ecosystem",R)
    (ASSETS/"ecosystem.svg").write_text(svgdoc(W,H,"The Firefly family constellation: Java/Spring, .NET, PyFly (highlighted), Rust, Go, Angular frontend, and GenAI around a shared core.","\n  ".join(b),amb=(cx,cy)))

def main():
    build_banner()
    for fn in (architecture,hexagonal,autoconf,lifecycle,patterns,ecosystem): fn()
    print("banner + 6 diagrams written to", ASSETS)
    print("WARNINGS:", *(WARN or ["none"]))

if __name__ == "__main__":
    main()
