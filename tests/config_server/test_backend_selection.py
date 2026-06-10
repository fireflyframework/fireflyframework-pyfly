# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Tests for _build_backend() — verifies that auto_configuration wires up the
correct backend type based on pyfly.config-server.backend.* config keys.

These tests MUST fail against the old dead-code wiring (where config_backend
always built a single-root FilesystemConfigBackend) and PASS after the fix.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from pyfly.config_server.auto_configuration import _build_backend
from pyfly.config_server.backend import ConfigBackend, FilesystemConfigBackend
from pyfly.core.config import Config

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg(**flat: object) -> Config:
    """Build a :class:`Config` from flat key→value pairs (dot-keys expanded)."""
    data: dict[str, object] = {}
    for k, v in flat.items():
        parts = k.split(".")
        node = data
        for part in parts[:-1]:
            node = node.setdefault(part, {})  # type: ignore[assignment]
        node[parts[-1]] = v  # type: ignore[index]
    return Config(data)  # type: ignore[arg-type]


def _make_git_repo(tmp_path: pathlib.Path) -> pathlib.Path:
    """Create a minimal local git repo with one committed config file.

    Returns the path to the repo root.  Skips automatically when GitPython is
    not installed — the caller is responsible for calling
    ``pytest.importorskip("git")`` before invoking this helper.
    """
    import git as gitlib  # noqa: PLC0415

    repo_dir = tmp_path / "origin"
    repo_dir.mkdir()
    repo = gitlib.Repo.init(repo_dir)
    repo.config_writer().set_value("user", "name", "Test").release()
    repo.config_writer().set_value("user", "email", "test@example.com").release()

    cfg_file = repo_dir / "svc-default.yaml"
    cfg_file.write_text(yaml.safe_dump({"env": "git-backend", "workers": 2}))
    repo.index.add(["svc-default.yaml"])
    repo.index.commit("initial config")

    # Ensure the branch is named "main".
    if repo.active_branch.name != "main":
        repo.head.reference = repo.create_head("main")
        repo.head.reset(index=True, working_tree=True)

    return repo_dir


# ---------------------------------------------------------------------------
# Fix 1 test A — backend.type=git → GitConfigBackend
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_backend_git_type_produces_git_backend(tmp_path: pathlib.Path) -> None:
    """_build_backend with backend.type=git returns a GitConfigBackend that reads
    properties committed in the repository.

    This test would FAIL against the old wiring (which always returned a plain
    FilesystemConfigBackend) because it verifies that a fetch on the returned
    backend reads data from a git-cloned working tree.
    """
    pytest.importorskip("git")

    origin = _make_git_repo(tmp_path)
    clone_dir = str(tmp_path / "clone")

    config = _cfg(
        **{
            "pyfly.config-server.backend.type": "git",
            "pyfly.config-server.backend.git.uri": str(origin),
            "pyfly.config-server.backend.git.label": "main",
            "pyfly.config-server.backend.git.clone-dir": clone_dir,
        }
    )

    backend = _build_backend(config)

    # Must satisfy the ConfigBackend protocol.
    assert isinstance(backend, ConfigBackend), "returned object does not satisfy ConfigBackend protocol"

    # Must NOT be a plain FilesystemConfigBackend — that would indicate the
    # old dead-code path is still in effect.
    from pyfly.config_server.adapters.git import GitConfigBackend  # noqa: PLC0415

    assert isinstance(backend, GitConfigBackend), (
        f"_build_backend should return GitConfigBackend when backend.type=git, but got {type(backend).__name__}"
    )

    # Data round-trip: fetch should return properties from the git repo.
    source = await backend.fetch("svc", "default")
    assert source is not None, "fetch() returned None — git clone may have failed"
    assert source.properties.get("env") == "git-backend"
    assert source.properties.get("workers") == 2


# ---------------------------------------------------------------------------
# Fix 1 test B — backend.search-locations → tiered FilesystemConfigBackend
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_backend_search_locations_merges_tiers(tmp_path: pathlib.Path) -> None:
    """_build_backend with backend.search-locations returns a tiered
    FilesystemConfigBackend that actually merges properties from multiple
    directories.

    This test would FAIL against the old wiring because the old code built a
    single-root backend (ignoring search-locations), so it would miss the
    keys from the lower-precedence directory.
    """
    # Set up two directories: domain (high precedence) and common (low).
    domain = tmp_path / "domain"
    common = tmp_path / "common"
    domain.mkdir()
    common.mkdir()

    # Write domain config — overrides "host", adds "domain_only".
    (domain / "app-prod.yaml").write_text(yaml.safe_dump({"host": "domain.db", "domain_only": True}))
    # Write common config — defines "host" (overridden) and "timeout".
    (common / "app-prod.yaml").write_text(yaml.safe_dump({"host": "common.db", "timeout": 30}))

    config = _cfg(
        **{
            "pyfly.config-server.backend.search-locations": [str(domain), str(common)],
        }
    )

    backend = _build_backend(config)

    assert isinstance(backend, FilesystemConfigBackend), (
        "_build_backend should return FilesystemConfigBackend for search-locations config, "
        f"but got {type(backend).__name__}"
    )

    source = await backend.fetch("app", "prod")
    assert source is not None, "fetch() returned None — check search-locations are accessible"

    # Domain overrides common.
    assert source.properties["host"] == "domain.db", "domain host must override common"
    # common-only key is inherited (fill-in semantics).
    assert source.properties["timeout"] == 30, "timeout key from common must be present"
    # domain-only key present.
    assert source.properties["domain_only"] is True


# ---------------------------------------------------------------------------
# Fix 1 test C — default (no type, no search-locations) → single-root FS backend
# ---------------------------------------------------------------------------


def test_build_backend_default_returns_filesystem_backend(tmp_path: pathlib.Path) -> None:
    """_build_backend with no special keys returns a single-root
    FilesystemConfigBackend (original behaviour preserved).
    """
    root = str(tmp_path / "cfgroot")

    config = _cfg(**{"pyfly.config-server.backend.root": root})

    backend = _build_backend(config)

    assert isinstance(backend, FilesystemConfigBackend), (
        f"_build_backend default path should return FilesystemConfigBackend, but got {type(backend).__name__}"
    )
    # The root directory must have been created.
    assert pathlib.Path(root).is_dir(), "backend root directory should be created"


def test_build_backend_no_config_uses_tempdir() -> None:
    """_build_backend with completely empty config falls back to a tempdir-rooted
    FilesystemConfigBackend without raising.
    """
    backend = _build_backend(Config())  # type: ignore[arg-type]
    assert isinstance(backend, FilesystemConfigBackend)
