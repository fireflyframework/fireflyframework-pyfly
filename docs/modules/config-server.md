# Config Server Guide

A lightweight, Spring-Cloud-Config-style **centralized configuration server**:
serve versioned config bundles (keyed by application + profile + label) to many
client services over HTTP.

---

## Table of Contents

1. [Introduction](#introduction)
2. [ConfigSource](#configsource)
3. [Backends](#backends)
4. [ConfigServer](#configserver)
5. [ConfigClient](#configclient)

---

## Introduction

The module has three parts:

- **`ConfigBackend`** — the storage SPI (a `Protocol`): `fetch`, `save`, `list`.
- **`ConfigServer`** — a framework-agnostic controller exposing config bundles
  over HTTP (`base_path = "/config"`).
- **`ConfigClient`** — fetches a bundle from a remote `ConfigServer` on startup.

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

**`FilesystemConfigBackend`** stores each bundle as
`<root>/<label>/<application>-<profile>.{yaml,yml,json}`. `fetch()` resolves the
first matching file (preferring YAML); `save()` writes back to **the same file
`fetch()` reads** (in its own format) and removes stale duplicate-format files,
so a save can never be silently shadowed by a pre-existing `.yaml`.

Implement `ConfigBackend` to back the server with a database, S3, Git, etc.:

```python
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

`fetch()` returns a Spring-Cloud-Config-compatible document
(`{name, profiles, label, propertySources: [...]}`), so existing Spring clients
can consume it. Mount the controller on your HTTP layer at `base_path` (`/config`).

---

## ConfigClient

A client service fetches its bundle from a remote server at startup:

```python
from pyfly.config_server import ConfigClient

client = ConfigClient(server_url="http://config:8888", application="orders", profile="prod")
properties = await client.fetch()
```

Merge the returned properties into your local `Config` before the context starts.
