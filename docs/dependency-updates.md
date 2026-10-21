# Dependency updates in v26.10.05

This release consolidates Dependabot [PR #171](https://github.com/fireflyframework/fireflyframework-pyfly/pull/171)
and [PR #157](https://github.com/fireflyframework/fireflyframework-pyfly/pull/157),
refreshes `uv.lock`, and updates the independent site and book toolchains.
Python 3.12 and 3.13 remain the tested versions. SQLAlchemy 2.0.50 and later
remain supported, including the separate 2.0 compatibility lane; the primary
lock selects 2.1.4. Beanie's existing Python `<3.14` restriction still applies
to the document-data extra. The proven asyncmy minimum stays at 0.2.11
while the lock updates to 0.2.16; the proposed 0.2.15 floor would needlessly
exclude an already qualified driver version.

## Presidio and cryptography

The published `presidio-anonymizer==2.2.364` requires cryptography below 49,
which conflicts with the patched cryptography 50 line. The `pii` extra therefore
uses the official upstream [merged fix](https://github.com/data-privacy-stack/presidio/pull/2231)
at immutable commit
[`5e2fcea990aa3b99660d2aea9121d0ba74a8940b`](https://github.com/data-privacy-stack/presidio/commit/5e2fcea990aa3b99660d2aea9121d0ba74a8940b).
This source revision retains the upstream distribution version `2.2.364`;
it is not a new published Presidio release. The lock resolves cryptography
50.0.2. PyFly does not override upstream package metadata or disable the PII extra.

Installing `pyfly[pii]` requires Git, access to GitHub, and the ability to build
the upstream Python source package with Poetry Core. These requirements apply
only when selecting `pii`; `full` does not include `pii`. Keep the direct source
reference when generating deployment locks or dependency inventories. An
index-only `presidio-anonymizer==2.2.364` entry is not an equivalent replacement.
The source pin can return to a normal version constraint after upstream publishes
a release that supports cryptography 50 and that release is qualified.

For an existing pip installation, first reinstall the named anonymizer from the
exact source reference. Pip can otherwise retain the old index package because
both sources report version `2.2.364`, even with `--upgrade`. Then upgrade PyFly to v26.10.07, which retains this source requirement:

```bash
python -m pip install --upgrade --force-reinstall "presidio-anonymizer @ git+https://github.com/data-privacy-stack/presidio.git@5e2fcea990aa3b99660d2aea9121d0ba74a8940b#subdirectory=presidio-anonymizer"
python -m pip install --upgrade "pyfly[pii,security] @ https://github.com/fireflyframework/fireflyframework-pyfly/releases/download/v26.10.07/pyfly-26.10.7-py3-none-any.whl"
python -m pip check
python -m spacy download en_core_web_sm
```

Configure `pyfly.logging.redaction.presidio.model: en_core_web_sm` when using
that smaller model; the default remains `en_core_web_lg`. See
[PII redaction](modules/logging.md#pii-redaction) for configuration.

The cryptography upgrade avoids the remaining 48.x
[PKCS7 decryption](https://github.com/pyca/cryptography/security/advisories/GHSA-g6cj-pr64-35w5),
[name-constraints](https://github.com/pyca/cryptography/security/advisories/GHSA-m2h6-j472-rp4c),
and [certificate path-building](https://github.com/pyca/cryptography/security/advisories/GHSA-jwv3-5hgf-82ww)
advisories. [Cryptography 49 removed support](https://cryptography.io/en/latest/changelog/#v49-0-0)
for macOS x86_64 and 32-bit Windows; users on those platforms must migrate to
a supported platform and Python installation before this upgrade.

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
not used by these workflows. Book builds use WeasyPrint 70 and CairoSVG 2.9.1
or later, including the upstream security fixes; both English and Spanish
PDF/EPUB editions are rebuilt from the software tag.

## Requested updates and resolved versions

The requested floor comes from the original Dependabot proposal. The resolved
version is the release lock, or the separately installed site/book toolchain.
Presidio anonymizer uses the source revision described above. SQLAlchemy's
proposal changed only a comment: its actual floor remains `>=2.0.50`.

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
| `presidio-analyzer` | `presidio-analyzer >=2.2.364` | `2.2.364` |
| `presidio-anonymizer` | `presidio-anonymizer>=2.2.364` | `2.2.364` |
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
