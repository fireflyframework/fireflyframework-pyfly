# Dependency updates in v26.10.09

This hotfix consolidates compatible updates from Dependabot
[PR #193](https://github.com/fireflyframework/fireflyframework-pyfly/pull/193).
The declared constraints are in `pyproject.toml`; `uv.lock` records the exact
repository selection. Applications must resolve and validate their own locks.
Python 3.12 and 3.13 remain the qualification targets.

## Native redaction and Presidio removal

The `pii` extra, Presidio backend and its source-pinned anonymizer dependency are
removed. Native regex redaction remains enabled by default. Remove `pii` from
your PyFly extras, remove `pyfly.logging.redaction.presidio`, and select
`engine: regex`. Legacy `auto` selects regex; explicit `presidio` is rejected.
Recreate the application environment from the new lock to remove unused
Presidio/spaCy packages. See the [logging migration](modules/logging.md#upgrading-from-the-removed-presidio-backend).

The previous Git/GitHub source-build and spaCy-model requirements no longer
apply to redaction. Historical v26.10.05–08 facts remain in the changelog;
do not reuse their removed backend installation instructions for this hotfix.

## Agentic compatibility boundaries

| Dependency | Hotfix selection | Compatibility boundary |
| --- | --- | --- |
| Pydantic AI | `==2.59.0` | Preserve provider extras and the native runtime contracts. |
| NumPy | `>=2.5.4` | The removed NLP dependency graph no longer imposes its NumPy restriction. |
| pydantic-monty | `==0.0.18` | Monty 1.2 uses a worker-pool API and removes the interpreter's `run_async`; a sandbox adapter migration needs separate qualification. |
| Ragas | `>=0.2.6,<0.2.7` | Ragas 0.4 introduces Instructor's `rich<15` constraint, conflicting with the CLI's Rich 15 baseline. |
| LangChain community | `>=0.3.31,<0.4` | Retain the compatible community adapter alongside Ragas 0.2.6; provider adapters resolve independently. |
| LangChain provider adapters | Anthropic `>=1.7.6`, Ollama `>=1.1.0`, OpenAI `>=1.7.2` | These 1.x adapters resolve with the retained community adapter; the community cap does not hold all LangChain packages at 0.3. |
| datasets | `>=5.0.1` | Retain the compatible release: 5.1 requires an hf-xet range incompatible with Pydantic AI's Hugging Face integration (`hf-xet<1.5`). |

The last row is a compatible minimum, not a claim that every newer release can
coexist with all extras. Let the resolver honor the complete graph rather than
forcing individual requested major versions. Keep optional evaluation and
sandbox dependencies out of installations that do not need them.

## Earlier v26.10.05 refresh

The earlier release consolidated Dependabot
[PR #171](https://github.com/fireflyframework/fireflyframework-pyfly/pull/171)
and [PR #157](https://github.com/fireflyframework/fireflyframework-pyfly/pull/157).
The following adapter notes and version table describe that earlier refresh;
the current hotfix's declared constraints and lock take precedence.

## Adapter and tooling compatibility

Redis 8 introduces a five-second default socket timeout. PyFly's Redis Streams
consumer explicitly keeps its blocking socket read unbounded so finite long
polls and `block_ms=0` remain valid; `stop()` cancels the consumer. Explicit
socket settings in the Redis URL retain precedence. Legacy stream response
shapes are selected for both RESP2 and RESP3. A real-server regression exercises
idle reads longer than five seconds, event delivery, and cancellation.

Click, jsonpath-ng, and mypy changes are accommodated without changing public
framework APIs. Testcontainers configuration tests isolate the Docker client;
real backend tests remain in the integration lanes. Installer tests isolate
PATH configuration from the user's shell profiles.

All 14 `actions/setup-python` uses are pinned to v7.0.0 commit
`5fda3b95a4ea91299a34e894583c3862153e4b97`. The removed `pip-install` input was
not used by these workflows. At that release, book builds used WeasyPrint 70 and CairoSVG 2.9.1
or later, including the upstream security fixes; both English and Spanish
PDF/EPUB editions are rebuilt from the software tag.

## Historical v26.10.05 requested updates and resolved versions

The requested floor comes from the original Dependabot proposal. The resolved
version is the release lock, or the separately installed site/book toolchain.
This table records v26.10.05, not the current hotfix lock. Removed Presidio
packages are omitted; their original release record remains in the changelog.
SQLAlchemy's proposal changed only a comment: its floor then remained `>=2.0.50`.

| Package | Dependabot requested floor | Resolved version |
|---|---|---|
| `aio-pika` | `aio-pika>=10.0.4` | `10.1.1` |
| `aiosmtpd` | `aiosmtpd>=1.4.6` | `1.4.6` |
| `alembic` | `alembic>=1.20.0` | `1.20.0` |
| `argon2-cffi` | `argon2-cffi>=25.1.0` | `25.1.0` |
| `asyncmy` | `asyncmy>=0.2.15` | `0.2.16` |
| `azure-storage-blob` | `azure-storage-blob>=12.30.3` | `12.31.0` |
| `beanie` | `beanie>=2.2.0` | `2.2.0` |
| `boto3` | `boto3>=1.43.103` | `1.43.114` |
| `click` | `click>=8.5.0` | `8.5.0` |
| `coverage` | `coverage[toml]>=7.16.1` | `7.16.2` |
| `croniter` | `croniter>=6.2.4` | `6.2.4` |
| `cryptography` | `cryptography>=50.0.1` | `50.0.2` |
| `fastapi` | `fastapi>=0.141.1` | `0.143.1` |
| `gitpython` | `GitPython>=3.1.62` | `3.2.1` |
| `granian` | `granian>=2.8.3` | `2.8.4` |
| `grpcio` | `grpcio>=1.84.0` | `1.84.0` |
| `markdown` | `Markdown>=3.11,<4` | `3.11.1` |
| `mkdocs-material` | `mkdocs-material>=9.7.7,<10` | `9.7.7` |
| `mypy` | `mypy>=2.3.1` | `2.4.0` |
| `opentelemetry-api` | `opentelemetry-api>=1.45.0` | `1.45.1` |
| `opentelemetry-exporter-otlp-proto-http` | `opentelemetry-exporter-otlp-proto-http>=1.45.0` | `1.45.1` |
| `opentelemetry-instrumentation-starlette` | `opentelemetry-instrumentation-starlette>=0.66b0` | `0.66b1` |
| `opentelemetry-sdk` | `opentelemetry-sdk>=1.45.0` | `1.45.1` |
| `pika` | `pika>=1.4.4` | `1.4.4` |
| `prometheus-client` | `prometheus-client>=0.26.0` | `0.26.0` |
| `pydantic` | `pydantic>=2.13.5` | `2.14.1` |
| `pygments` | `Pygments>=2.21.0,<3` | `2.21.0` |
| `pyjwt` | `pyjwt[crypto]>=2.15.0` | `2.15.1` |
| `pymdown-extensions` | `pymdown-extensions>=12.1,<13` | `12.2` |
| `pyotp` | `pyotp>=2.10.0` | `2.10.0` |
| `pytest` | `pytest>=9.1.1` | `9.1.1` |
| `pytest-asyncio` | `pytest-asyncio>=1.4.0` | `1.4.0` |
| `python-multipart` | `python-multipart>=0.0.32` | `0.0.32` |
| `redis` | `redis[hiredis]>=8.1.0` | `8.1.0` |
| `respx` | `respx>=0.23.1` | `0.23.1` |
| `ruff` | `ruff>=0.16.9` | `0.17.0` |
| `sqlalchemy` | `sqlalchemy[asyncio]>=2.0.50 (lock >=2.1.1)` | `2.1.4` |
| `starlette` | `starlette>=1.7.0` | `1.8.0` |
| `structlog` | `structlog>=26.1.0` | `26.1.0` |
| `testcontainers` | `testcontainers>=4.15.0` | `4.15.0` |
| `uvicorn` | `uvicorn[standard]>=0.54.0` | `0.54.0` |
| `weasyprint` | `WeasyPrint>=70.0,<71` | `70.0` |
| `websockets` | `websockets>=17.1` | `17.2` |
| `cairosvg` (additional security update) | `CairoSVG>=2.9.1,<3` | `2.9.1` |

CairoSVG is additionally raised from `>=2.9,<3` to `>=2.9.1,<3` to exclude
[the affected 2.9.0 release](https://github.com/Kozea/CairoSVG/security/advisories/GHSA-c3jg-qh8m-j3h2).
Package minimums allow compatible newer releases; use `uv.lock` for the
repository's exact dependency selection. Consumers should maintain and validate
their own application lockfiles when upgrading.
