# Config Server Guide

A lightweight, Spring-Cloud-Config-style **centralized configuration server**:
serve versioned config bundles (keyed by application + profile + label) to many
client services over HTTP.

---

## Table of Contents

1. [Introduction](#introduction)
2. [ConfigSource](#configsource)
3. [Backends](#backends)
   - [InMemoryConfigBackend](#inmemoryconfigbackend)
   - [FilesystemConfigBackend](#filesystemconfigbackend)
   - [Tiered search locations](#tiered-search-locations)
   - [GitConfigBackend](#gitconfigbackend)
   - [Custom backends](#custom-backends)
4. [ConfigServer](#configserver)
5. [ConfigClient](#configclient)

---

## Introduction

The module has three parts:

- **`ConfigBackend`** — the storage SPI (a `Protocol`): `fetch`, `save`, `list`.
- **`ConfigServer`** — a framework-agnostic controller exposing config bundles
  over HTTP.
- **`ConfigClient`** — fetches a bundle from a remote `ConfigServer` on startup.

### Enabling the server

Set `pyfly.config-server.enabled=true` and the framework auto-configures a
`ConfigServer` (backed by a `FilesystemConfigBackend`) and **mounts its HTTP
routes automatically** — you do not have to wire any controller by hand:

```yaml
pyfly:
  config-server:
    enabled: true
    base-path: ""                          # optional URL prefix (default: none)
    backend:
      root: "/etc/pyfly/config"            # persistent backend directory
```

The routes are mounted by a post-start rescan (`config_server/wiring.py`) because the
`ConfigServer` bean is created during context start. See
[ConfigServer](#configserver) for the route table.

---

## ConfigSource

A bundle is identified by `application` + `profile` + `label`:

```python
from pyfly.config_server import ConfigSource

ConfigSource(
    application="orders",
    profile="prod",
    label="main",                 # defaults to "main"
    properties={"db.url": "..."},
)
```

---

## Backends

Two backends ship out of the box:

```python
from pyfly.config_server import InMemoryConfigBackend, FilesystemConfigBackend

backend = InMemoryConfigBackend()                 # great for tests
backend = FilesystemConfigBackend("/etc/pyfly")   # reads/writes files
```

### InMemoryConfigBackend

Dict-backed, thread-safe via `asyncio.Lock`. Ideal for unit tests.

### FilesystemConfigBackend

**`FilesystemConfigBackend`** stores each bundle as
`<root>/<label>/<application>-<profile>.{yaml,yml,json}` (falling back to
`<root>/<application>-<profile>.*` when the labeled file is absent). `fetch()`
resolves the first matching file (preferring YAML); `save()` writes back to **the
same file `fetch()` reads** (in its own format) and removes stale duplicate-format
files, so a save can never be silently shadowed by a pre-existing `.yaml`.

When auto-configured, the backend `root` is **configurable and persistent** via
`pyfly.config-server.backend.root` (or `pyfly.config-server.native.search-locations`),
so saved config survives restarts. Only when neither is set does it fall back to a
throwaway tempdir (`config_server/auto_configuration.py`).

### Tiered search locations

Pass `search_locations` (a list of directories, **highest-precedence first**) to
merge config from multiple layers.  The convention is `[domain, core, common]` so
domain settings override core, which override common; keys present only in a
lower-precedence location are **inherited** (fill-in semantics).

```python
backend = FilesystemConfigBackend(
    domain_dir,
    search_locations=[domain_dir, core_dir, common_dir],
)
```

Configure via YAML (comma-separated or YAML list):

```yaml
pyfly:
  config-server:
    backend:
      search-locations:
        - /etc/pyfly/domain
        - /etc/pyfly/core
        - /etc/pyfly/common
```

`save()` and `list()` always operate on the **first** (primary / highest-
precedence) location.

### GitConfigBackend

A Git-backed backend that clones a repository and delegates file reads to
`FilesystemConfigBackend` over the working tree.

```python
from pyfly.config_server.adapters.git import GitConfigBackend

backend = GitConfigBackend(
    "https://github.com/my-org/config-repo.git",
    label="main",          # branch / tag / SHA
    clone_dir="/tmp/cfg",  # optional; defaults to a tempdir
)
```

Requires GitPython: `pip install pyfly[config-server-git]`.

**Saving** writes the file into the working tree and commits it locally.
Pushing to the remote is **out of scope** — call `await backend.refresh()` to
pull the latest commits from `origin`.

Configure via YAML:

```yaml
pyfly:
  config-server:
    backend:
      type: git
      git:
        uri: "https://github.com/my-org/config-repo.git"
        label: main
        clone-dir: /var/lib/pyfly/git-config   # optional
```

When `backend.type=git` is set but GitPython is not installed, the server logs
a warning and falls back to `FilesystemConfigBackend`.

### Custom backends

Implement `ConfigBackend` to back the server with Consul, Vault, etcd,
a database, S3, or any other store:

```python
from pyfly.config_server.backend import ConfigBackend, ConfigSource

@runtime_checkable
class ConfigBackend(Protocol):
    async def fetch(self, application: str, profile: str, label: str = "main") -> ConfigSource | None: ...
    async def save(self, source: ConfigSource) -> None: ...
    async def list(self) -> list[ConfigSource]: ...
```

---

## ConfigServer

```python
from pyfly.config_server import ConfigServer, FilesystemConfigBackend

server = ConfigServer(FilesystemConfigBackend("/etc/pyfly"))

bundle = await server.fetch("orders", "prod")     # Spring-Cloud-Config shaped dict
await server.save("orders", "prod", {"db.url": "postgres://..."})
all_sources = await server.list()
```

`fetch(application, profile="default", label="main")` returns a
Spring-Cloud-Config-compatible document (`{name, profiles, label, propertySources: [...]}`),
so existing Spring clients can consume it. The `propertySources` array contains the
**full overlay set** (highest priority first), deduplicated:

1. `{application}/{profile}`
2. `{application}/default`
3. `application/{profile}` — the shared `application` bundle for the profile
4. `application/default`

A client merges these with the first source winning. `fetch()` returns `None` only when
every overlay is absent.

### HTTP routes

When `pyfly.config-server.enabled=true`, the framework mounts these Starlette routes
(under the optional `pyfly.config-server.base-path` prefix, empty by default):

| Method | Path | Purpose |
|---|---|---|
| `GET`  | `/{application}/{profile}` | Fetch the merged config (label `main`); 404 if absent |
| `GET`  | `/{application}/{profile}/{label}` | Fetch for a specific label |
| `POST` | `/{application}/{profile}` | Save a config bundle (JSON body) |
| `POST` | `/{application}/{profile}/{label}` | Save for a specific label |
| `GET`  | `/_list` | List stored bundles |

The routes are built by `make_starlette_config_server_routes()`
(`config_server/adapters/starlette.py`) and wired by `config_server/wiring.py`.

---

## ConfigClient

A client service fetches its bundle from a remote server at startup:

```python
from pyfly.config_server import ConfigClient

client = ConfigClient(
    url="http://config:8888",   # keyword-only; all args are keyword-only
    application="orders",
    profile="prod",
    label="main",               # optional, defaults to "main"
    username=None,              # optional HTTP basic auth
    password=None,
    http_client=None,           # optional: inject an httpx.AsyncClient
)
properties = await client.fetch()   # flattened {dotted_key: value} dict
```

`fetch()` requires `httpx` (`pip install pyfly[client]`). It GETs
`{url}/{application}/{profile}/{label}`, then merges the document's `propertySources`
in **reverse** order (Spring lists highest priority first) so the highest-priority
source wins. A non-200 response logs a warning and returns `{}`.

### Transport injection

Pass `http_client` (an `httpx.AsyncClient`) to reuse a connection pool or to
drive the client against an ASGI app in tests:

```python
import httpx
from starlette.applications import Starlette

from pyfly.config_server.adapters.starlette import make_starlette_config_server_routes

app = Starlette(routes=make_starlette_config_server_routes(server))

async with httpx.AsyncClient(
    transport=httpx.ASGITransport(app=app),
    base_url="http://config",
) as http_client:
    client = ConfigClient(
        url="http://config",
        application="orders",
        profile="prod",
        http_client=http_client,
    )
    props = await client.fetch()
```

When `http_client` is injected the caller owns its lifecycle — `fetch()` does
**not** close it.

**Invoked automatically at startup.** You normally do not call `ConfigClient` directly:
`PyFlyApplication` invokes it during bootstrap when `pyfly.cloud.config.uri` (or
`pyfly.config.import`) is set, merging the result into the application `Config` as a
high-precedence source. See
[Remote Config Import](configuration.md#remote-config-import-config-server) in the
configuration guide.
