# PyFly by Example — Book Design Spec

- **Date:** 2026-06-07
- **Status:** Approved (design) — ready for implementation planning
- **Branch:** `book/pyfly-by-example` (based on `main` @ `1f9d1f3`)
- **Deliverables:** a polished, illustrated digital book in **EPUB** and **PDF**

---

## 1. Summary

A project-driven, richly illustrated book that teaches the **PyFly** framework end to end — the
"*Django by Example*" of PyFly. Intermediate Python developers build **one real fintech system,
"Lumen" (a digital wallet · ledger · transfers platform), that grows chapter by chapter from a
single service into an event-sourced, saga-orchestrated set of microservices**, learning the whole
framework by doing.

This book is **not** a reference manual (the `docs/` tree already serves that role). It is a
narrative, hands-on learning journey with production-grade formatting and original artwork.

## 2. Goals / Non-Goals

**Goals**
- Teach PyFly's full surface through a coherent, growing project.
- Packt-grade production quality: consistent typography, syntax-highlighted listings, callouts,
  captioned figures, original cover and illustrations.
- Ship both **EPUB** (screen) and **PDF** (print-style, 7.5″×9.25″).
- Code listings are **real, verified PyFly code** checked against the framework in this repo.

**Non-Goals**
- Not auto-generated from docstrings; written prose.
- No photoreal / AI-raster art — **all illustrations are hand-authored vector SVG**.
- Not a Spring tutorial — Spring is referenced only via "Spring parity" callouts for migrators.

## 3. Audience & Voice

- **Primary reader:** intermediate Python developer, comfortable with `async`/`await` and type
  hints, new to PyFly. Brisk pace; no hand-holding on language fundamentals.
- **Secondary:** Spring Boot / Java developers migrating to Python (served by "Spring parity"
  callouts and Appendix A).
- **Voice:** warm, precise, confident; second person ("you build…"); short paragraphs; every
  concept motivated by the Lumen project before the API is shown.

## 4. Running Project: "Lumen"

A digital wallet platform. Evolution arc (mirrors the 5 parts):

1. **One service** — a Lumen HTTP API (wallets, balances).
2. **+ domain core** — persistence, DDD aggregates (Wallet, Money), CQRS for transfers.
3. **+ event-driven** — domain events, an event-sourced **ledger**, messaging.
4. **split into services** — Wallet · Payments · Notifications; HTTP clients; a cross-service
   **transfer saga** with compensation.
5. **production** — security/identity, observability + admin, testing, scheduling/notifications/
   webhooks, plugins/rule-engine, deployment.

## 5. Visual Identity (locked)

- **Cover:** "Daylight" — bright, classic tech-book layout using the **real PyFly logo**
  (`assets/pyfly-logo.png`) as the hero on a light gradient, with a green title band carrying
  "by Example", subtitle, and the series tagline; amber rule.
- **Illustration style:** "Friendly Flat" — solid shapes, rounded forms, soft shadows.
- **Interior:** "Modern" — sans-serif body, airy spacing, rounded callouts; light code listings
  with a file-name tab and "Listing N.N" caption; captioned Friendly-Flat figures.
- **Mascot:** **Sparky** — the friendly green snake (derived from the official logo), appearing in
  chapter-opener badges and callout icons.
- **Palette tokens:** core green `#43b02a`; bright `#5fd13a`; light lime `#86dd4c`/`#a7e76a`;
  deep green `#2c8a1c`/`#1f5e16`; amber `#ffc24b`/`#ffd86b`; ink `#1c2420`; page `#fffef9`;
  code surface `#f7faf3`; muted `#7a8472`.
- **Code highlight theme (light, print-safe):** keyword `#cf222e`, string `#0a3069`, comment
  `#6e7781`, function `#8250df`, decorator `#953800`, number `#0550ae`, type/builtin `#116329`.

## 6. Content Plan

**Front matter:** title page (logo), copyright (Apache-2.0, Firefly Software Foundation), preface
("who this is for / what you'll build / how to use this book"), conventions, "Meet Sparky", the
roadmap illustration, table of contents.

**Chapters (18, across 5 parts)** — each line: *what you build · key modules*

*Part I — Foundations*
1. **Why PyFly?** install, `pyfly new`, run, banner, `/docs` · core, cli, starters
2. **Dependency Injection & the App Context** · container, context
3. **Configuration, Profiles & Secrets** · config (+ config-server preview)
4. **Your First HTTP API** controllers, `Valid[T]`, RFC-7807, OpenAPI · web, validation, kernel, server

*Part II — Modeling & Persisting*
5. **Persistence & the Repository Pattern** · data, data-relational
6. **Domain-Driven Design** aggregates, Money, invariants · domain
7. **CQRS: Commands & Queries** · cqrs

*Part III — Event-Driven*
8. **Domain Events & EDA** · eda
9. **Event Sourcing the Ledger** · eventsourcing
10. **Messaging: Kafka & RabbitMQ** · messaging

*Part IV — Into Microservices*
11. **Splitting the Monolith + HTTP Clients** · client
12. **Sagas, Workflows & TCC** · transactional
13. **Caching & Resilience** · cache, resilience

*Part V — Secure · Observe · Ship*
14. **Security, Sessions & Identity** · security, session, idp
15. **Observability + Admin** logging/PII, metrics, tracing, actuator, admin, aop
16. **Testing PyFly Apps** pytest, Testcontainers · testing
17. **Scheduling, Notifications, Webhooks & Callbacks** · scheduling, notifications, webhooks, callbacks
18. **Extending PyFly & Going to Production** plugins, rule-engine, config-server, i18n, websocket, shell, openapi, deploy

**Appendices:** A) Spring Boot → PyFly cheat-sheet · B) MongoDB / document data · C) ECM & content
· D) CLI & troubleshooting · Glossary · Index.

**Per-chapter anatomy:** opener (Sparky badge + number + title) → intro ("what you'll build") →
numbered sections → code listings (file tab + caption) → figures (caption) → callouts
(Note/Tip/Warning + Spring parity) → "What you built" recap → "Try it yourself" exercises.

## 7. Build Architecture

**Repo layout (all under `book/`):**
```
book/
  book.yaml                 # metadata: title, subtitle, author, trim, part/chapter order
  manuscript/
    00-front/               # title, copyright, preface, conventions, meet-sparky
    01-why-pyfly.md … 18-*.md
    90-appendix-a.md … glossary.md
  art/
    cover.svg               # Daylight cover (embeds the real logo)
    logo/pyfly-logo.png     # official logo (source of truth)
    mascot/sparky-*.svg     # Sparky poses
    openers/chNN.svg        # per-chapter opener illustrations
    figures/NN-x.svg        # technical diagrams (Friendly Flat)
    tokens.css              # shared palette variables
  theme/
    book.css                # shared screen/EPUB styles (Modern)
    print.css               # @page size, running heads, page numbers (PDF)
    pygments.css            # light code-highlight theme
  build/
    build.py                # orchestrator: md -> html -> {epub, pdf}
    md.py                   # markdown + extensions + custom callout/figure syntax
    epub.py                 # EPUB3 assembler (stdlib zipfile; embeds SVG; cover.png)
    pdf.py                  # WeasyPrint HTML/CSS -> PDF
    verify_code.py          # extract listings; import/type-check vs real pyfly
    run.sh                  # wrapper exporting DYLD_FALLBACK_LIBRARY_PATH + venv
  dist/                     # pyfly-by-example.epub / .pdf  (git-ignored build output)
  .venv/                    # isolated build venv, Python 3.12 (git-ignored)
```

**Pipeline:** Markdown → HTML via `markdown` (fenced_code, tables, attr_list, toc, footnotes) +
`pygments` (codehilite). Custom block syntax compiles callouts and figures. EPUB3 is assembled
with the Python **stdlib** (`zipfile` + XML for `content.opf`/`nav.xhtml`), embedding SVG natively
and a raster `cover.png` (via `cairosvg` or Pillow). The **same HTML/CSS** renders the PDF via
**WeasyPrint** with `print.css` (trim **7.5″×9.25″**, running heads, page numbers, PDF bookmarks).

**Code authenticity:** every listing is real PyFly code, extracted by `verify_code.py` and
import/type-checked (and run where feasible) against the framework installed in this repo. No
invented APIs.

## 8. Formats

- **EPUB3:** reflowable; embedded fonts optional; SVG figures inline; raster cover; valid
  `nav.xhtml` TOC + `content.opf` spine. Target readers: Apple Books, Calibre, Kindle (via convert).
- **PDF:** 7.5″×9.25″, generous margins, running heads (book title / chapter), outer page numbers,
  chapter page-breaks, clickable TOC + bookmarks.

## 9. Tooling & Environment (verified 2026-06-07)

- **Build venv:** `book/.venv` on **Python 3.12.13** (created from the project interpreter).
- **pip packages:** `markdown 3.9`, `pygments 2.20.0`, `weasyprint 66`, `cairosvg 2.8.2`,
  `Pillow 11.3`.
- **Homebrew libs:** `pango cairo gdk-pixbuf libffi` (Apple-Silicon prefix `/opt/homebrew`).
- **Critical:** the build must run with
  `DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib:/usr/local/lib` so cairo/pango load. Handled by
  `book/build/run.sh`.
- **Verified:** `cairosvg.svg2png` and `weasyprint … write_pdf` both succeed.

## 10. Execution Phases (checkpoints between each)

- **Phase 0 — vertical slice:** scaffold `book/`, build pipeline, theme, **cover (real logo) +
  Sparky + diagram kit**, and write **front matter + Chapter 1 fully**, built to EPUB **and** PDF.
  Reviewed as the real artifact to lock voice + formatting.
- **Phases 1–5 — one Part at a time:** chapters + art; rebuild; review.
- **Final:** full build, EPUB validation, polish, deliver EPUB + PDF.

## 11. Definition of Done (per chapter)

- Builds cleanly into both EPUB and PDF with no layout overflow.
- All listings verified against the real framework.
- Opener illustration + at least one figure + appropriate callouts present.
- Recap + exercises included; cross-references resolve; TOC/bookmarks updated.

## 12. Risks & Mitigations

- **WeasyPrint system libs / DYLD** → pinned via `run.sh`; verified working.
- **EPUB reader SVG variance** → keep SVG conservative; provide raster cover; validate output.
- **Book size / effort** → strict phasing with review checkpoints; repeatable per-chapter template.
- **Framework API drift during writing** → `verify_code.py` re-checks every listing on each build.

## 13. Open Items

- **Author line:** defaults to "Firefly Software Foundation" unless changed.
- **Exercise solutions:** appendix vs inline (decide at Phase 0 review).
- **Index generation:** automated term index in Phase Final (nice-to-have).
