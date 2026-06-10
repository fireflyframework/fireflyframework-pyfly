# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Tests for GitConfigBackend.

All tests use a local (on-disk) bare/non-bare repository — no network access.
The suite is skipped automatically when GitPython is not installed.
"""

from __future__ import annotations

import pathlib

import pytest

git = pytest.importorskip("git")

import yaml  # noqa: E402

from pyfly.config_server.adapters.git import GitConfigBackend  # noqa: E402
from pyfly.config_server.backend import ConfigSource  # noqa: E402


def _make_repo(tmp_path: pathlib.Path, branch: str = "main") -> pathlib.Path:
    """Create a local git repository with one committed config file."""
    repo_dir = tmp_path / "origin"
    repo_dir.mkdir()

    repo = git.Repo.init(repo_dir)
    # git requires at least name + email to commit.
    repo.config_writer().set_value("user", "name", "Test").release()
    repo.config_writer().set_value("user", "email", "test@example.com").release()

    # Write an orders-prod.yaml on the repo root (label "main" maps to root
    # in _path_candidates_for, but FilesystemConfigBackend also checks the
    # label sub-directory; place it at root for simplicity).
    orders_yaml = repo_dir / "orders-prod.yaml"
    orders_yaml.write_text(yaml.safe_dump({"db.url": "postgres://prod", "workers": 4}))

    repo.index.add(["orders-prod.yaml"])
    repo.index.commit("initial config")

    # Rename default branch to the requested name (git may default to
    # "master" depending on the system git config).
    if repo.active_branch.name != branch:
        repo.head.reference = repo.create_head(branch)
        repo.head.reset(index=True, working_tree=True)

    return repo_dir


@pytest.mark.asyncio
async def test_git_backend_fetch(tmp_path: pathlib.Path) -> None:
    """GitConfigBackend.fetch reads properties committed in the repo."""
    origin = _make_repo(tmp_path)
    clone_dir = str(tmp_path / "clone")

    backend = GitConfigBackend(str(origin), label="main", clone_dir=clone_dir)
    source = await backend.fetch("orders", "prod")

    assert source is not None
    assert source.application == "orders"
    assert source.profile == "prod"
    assert source.properties["db.url"] == "postgres://prod"
    assert source.properties["workers"] == 4


@pytest.mark.asyncio
async def test_git_backend_list(tmp_path: pathlib.Path) -> None:
    """GitConfigBackend.list enumerates committed config files."""
    origin = _make_repo(tmp_path)
    clone_dir = str(tmp_path / "clone")

    backend = GitConfigBackend(str(origin), label="main", clone_dir=clone_dir)
    sources = await backend.list()

    assert any(s.application == "orders" and s.profile == "prod" for s in sources)


@pytest.mark.asyncio
async def test_git_backend_save_commits(tmp_path: pathlib.Path) -> None:
    """GitConfigBackend.save writes the file and creates a local commit."""
    origin = _make_repo(tmp_path)
    clone_dir = str(tmp_path / "clone")

    backend = GitConfigBackend(str(origin), label="main", clone_dir=clone_dir)
    # First fetch to initialise the clone.
    await backend.fetch("orders", "prod")

    new_source = ConfigSource(
        application="payments",
        profile="prod",
        label="main",
        properties={"gateway": "stripe", "retries": 3},
    )
    await backend.save(new_source)

    # The property is now readable through a fresh fetch.
    fetched = await backend.fetch("payments", "prod")
    assert fetched is not None
    assert fetched.properties["gateway"] == "stripe"

    # A commit must have been created.
    import git as gitlib

    repo = gitlib.Repo(clone_dir)
    last_msg = repo.head.commit.message
    assert "payments" in last_msg or "pyfly" in last_msg


@pytest.mark.asyncio
async def test_git_backend_save_updates_existing(tmp_path: pathlib.Path) -> None:
    """GitConfigBackend.save updates an existing file (not creates a duplicate)."""
    origin = _make_repo(tmp_path)
    clone_dir = str(tmp_path / "clone")

    backend = GitConfigBackend(str(origin), label="main", clone_dir=clone_dir)

    updated = ConfigSource(
        application="orders",
        profile="prod",
        label="main",
        properties={"db.url": "postgres://prod-v2", "workers": 8},
    )
    await backend.save(updated)

    fetched = await backend.fetch("orders", "prod")
    assert fetched is not None
    assert fetched.properties["db.url"] == "postgres://prod-v2"
    assert fetched.properties["workers"] == 8


@pytest.mark.asyncio
async def test_git_backend_refresh_no_remote(tmp_path: pathlib.Path) -> None:
    """refresh() is a no-op (not an error) when the clone has no remote."""
    origin = _make_repo(tmp_path)
    clone_dir = str(tmp_path / "clone")

    backend = GitConfigBackend(str(origin), label="main", clone_dir=clone_dir)
    await backend.fetch("orders", "prod")  # init clone

    # Remove the remote so refresh() must skip gracefully.
    import git as gitlib

    repo = gitlib.Repo(clone_dir)
    repo.delete_remote(repo.remote("origin"))

    await backend.refresh()  # must not raise


@pytest.mark.asyncio
async def test_git_backend_missing_returns_none(tmp_path: pathlib.Path) -> None:
    """fetch() returns None for a file that doesn't exist in the repo."""
    origin = _make_repo(tmp_path)
    clone_dir = str(tmp_path / "clone")

    backend = GitConfigBackend(str(origin), label="main", clone_dir=clone_dir)
    result = await backend.fetch("nonexistent", "dev")
    assert result is None
