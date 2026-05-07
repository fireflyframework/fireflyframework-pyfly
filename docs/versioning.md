# Versioning

PyFly uses **Calendar Versioning** ([CalVer](https://calver.org/)) to stay
aligned with the rest of the Firefly Framework family (Java, .NET, Go) which
all adopted CalVer in May 2026.

---

## Version Format

All PyFly versions follow the `YY.MM.PATCH` scheme:

| Component | Meaning | Example |
|-----------|---------|---------|
| `YY` | Two-digit year | `26` (2026) |
| `MM` | Two-digit month of the release | `05` (May) |
| `PATCH` | Patch number within the month | `01`, `02`, `03`, … |

**Examples:** `26.05.01`, `26.05.02`, `26.06.01`, `27.01.01`.

A new patch within the same month bumps `PATCH` (`26.05.01` → `26.05.02`). The
first release of the next month resets `PATCH` to `01` and advances `MM`
(`26.05.02` → `26.06.01`). January of a new year advances `YY`
(`26.12.04` → `27.01.01`).

---

## Two notations

Because [PEP 440](https://peps.python.org/pep-0440/) normalises numeric
components by stripping leading zeros, PyFly carries the version in two
notations that always reference the same release:

| Surface | Notation | Example |
|---------|----------|---------|
| Git tag, GitHub release, README badge, banner, `__version__` | leading-zero (matches Java/.NET/Go siblings) | `v26.05.01` / `26.05.01` |
| `pyproject.toml`, wheel filename, Python tooling (uv, pip, hatchling) | PEP 440 normalised | `26.5.1` / `pyfly-26.5.1-py3-none-any.whl` |

Both forms are interchangeable when filling in URLs and dependency
specifiers — the normalised form lands on the wheel filename, the
leading-zero form lands on the tag. Tools that accept PEP 440 specifiers
treat them identically.

---

## Pre-release suffixes (optional)

If a release is published before all month-end testing has finished, PyFly
appends a PEP 440 pre-release suffix to the package version:

| Stage | Tag | Package version |
|-------|-----|-----------------|
| Alpha | `v26.05.01-alpha.1` | `26.5.1a1` |
| Beta | `v26.05.01-beta.1` | `26.5.1b1` |
| Release Candidate | `v26.05.01-rc.1` | `26.5.1rc1` |
| Final | `v26.05.01` | `26.5.1` |

Most months ship a single final release; the pre-release stages exist for the
rare case where a substantial change needs an additional review window.

---

## Release history

| Version | Date | Notes |
|---------|------|-------|
| `26.05.01` | 2026-05-07 | **CalVer migration.** Full Java framework parity: rewritten transactional engine (Saga + Workflow + TCC), nine new modules (eventsourcing, callbacks, webhooks, notifications, IDP, ECM, plugins, rule engine, config server), 12 new third-party adapters, four new client protocols (SOAP/gRPC/GraphQL/WebSocket), 16 domain validators. |
| `0.3.0-M1` | 2026-05-07 | (Superseded by 26.05.01.) Initial v0.3.0 milestone — same payload as 26.05.01 under the previous SemVer scheme. |
| `0.2.0-M11` | 2026-03-01 | Thread-safety, correctness, and robustness audit: 18 fixes across DI, web, resilience, security, data. |
| `0.2.0-M10` | 2026-02-28 | uv-first tooling: PEP 735 dependency-groups, uv-native CI/templates/installer, tool-neutral error messages. |
| `0.2.0-M9` | 2026-02-20 | Method security, `@transactional`, K8s probes, Pydantic config, soft delete/versioning. |
| `0.2.0-M8` | 2026-02-20 | HttpSecurity DSL, OAuth2 login flow, data auditing, `@query` for MongoDB, SSE. |
| `0.2.0-M7` | 2026-02-19 | Comprehensive audit: WebSocket, OAuth2, session, i18n, XML, bug fixes. |
| `0.2.0-M6` | 2026-02-19 | ASGI pathsend extension fix for Granian. |
| `0.2.0-M5` | 2026-02-19 | Auto-configuration audit (8 new auto-config classes), stdlib logging fallback, post-processor deduplication. |
| `0.2.0-M4` | 2026-02-18 | Admin dashboard overhaul, pure ASGI middleware (anyio fix), built-in metrics, bean categories, mapping/trace/logger enhancements. |
| `0.2.0-M3` | 2026-02-18 | Clean server startup, graceful shutdown, admin dashboard enhancements, mypy strict compliance. |
| `0.2.0-M2` | 2026-02-18 | Application server architecture, FastAPI adapter, Granian/Hypercorn support. |

The 0.x → CalVer migration was a one-time renumbering that does **not**
change the package contents — `26.05.01` ships exactly what `0.3.0-M1`
shipped, with the version metadata updated.

---

## Reading the version at runtime

```python
import pyfly
print(pyfly.__version__)  # → "26.05.01"
```

```bash
pyfly --version            # → 26.05.01
```

The startup banner displays the leading-zero form:

```
:: PyFly Framework :: (v26.05.01) (Python 3.13.9)
```

---

## Why CalVer?

The Firefly Framework family (Java, .NET, Go, Python) ships from a single
multi-repo release cadence — every port snaps to the same monthly version so
operators running heterogeneous services can identify "which Firefly month"
each one is on at a glance. SemVer's "is this a breaking change?" semantics
remain valuable inside individual modules but at the framework level the
month-based promise (regular releases, predictable cadence) is the more
useful contract.
