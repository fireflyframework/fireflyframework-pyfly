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
"""Git-backed config backend — wraps a local or remote Git repository.

Config files are read from the working tree of a cloned (or locally accessed)
repository; writes are committed locally. Pushing to a remote is **out of
scope** — call ``refresh()`` after a remote push to pull the latest commits.

Requires GitPython: ``pip install pyfly[config-server-git]``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import tempfile
from typing import Any

from pyfly.config_server.backend import ConfigSource, FilesystemConfigBackend

_logger = logging.getLogger(__name__)


def _require_git() -> Any:
    """Import and return the ``git`` module, raising a friendly ImportError if absent."""
    try:
        import git

        return git
    except ImportError as exc:
        msg = "GitConfigBackend requires GitPython — `pip install pyfly[config-server-git]`"
        raise ImportError(msg) from exc


class GitConfigBackend:
    """``ConfigBackend`` backed by a Git repository.

    On first use the repository is cloned (or, for a local ``file://`` / path
    URI, the on-disk repo is reused) into *clone_dir* (or a tempdir when
    *clone_dir* is ``None``).  The working tree is then delegated to a
    :class:`~pyfly.config_server.backend.FilesystemConfigBackend` so all the
    file-search and merge logic is shared.

    Parameters
    ----------
    uri:
        Any URI accepted by ``git clone``: ``https://``, ``git@``, or a local
        path (``/path/to/repo`` or ``file:///path/to/repo``).
    label:
        Branch (or tag / SHA) to check out.  Defaults to ``"main"``.
    clone_dir:
        Where to clone the repository.  When *None* a temporary directory is
        created automatically and cleaned up when the process exits.
    """

    def __init__(
        self,
        uri: str,
        *,
        label: str = "main",
        clone_dir: str | None = None,
    ) -> None:
        self._uri = uri
        self._label = label
        self._clone_dir = clone_dir
        self._repo: Any = None  # git.Repo, set on first _ensure_repo() call
        self._fs: FilesystemConfigBackend | None = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_repo(self) -> FilesystemConfigBackend:
        """Lazily clone the repo and return (or return the cached) FS backend."""
        if self._fs is not None:
            return self._fs

        git = _require_git()

        work_dir = self._clone_dir or tempfile.mkdtemp(prefix="pyfly-git-config-")
        _logger.debug("GitConfigBackend: cloning %s → %s (label=%s)", self._uri, work_dir, self._label)
        self._repo = git.Repo.clone_from(self._uri, work_dir)
        # Checkout the requested label (branch / tag / sha).
        try:
            self._repo.git.checkout(self._label)
        except git.GitCommandError as exc:
            _logger.warning("GitConfigBackend: could not checkout %r: %s", self._label, exc)

        self._fs = FilesystemConfigBackend(work_dir)
        return self._fs

    async def _run_sync(self, fn: Any, *args: Any) -> Any:
        """Run a synchronous callable in the default executor."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, fn, *args)

    async def _ensure_repo_async(self) -> FilesystemConfigBackend:
        """Async wrapper that returns a typed FilesystemConfigBackend."""
        result: FilesystemConfigBackend = await self._run_sync(self._ensure_repo)
        return result

    # ------------------------------------------------------------------
    # ConfigBackend protocol
    # ------------------------------------------------------------------

    async def fetch(self, application: str, profile: str, label: str = "main") -> ConfigSource | None:
        fs = await self._ensure_repo_async()
        return await fs.fetch(application, profile, label)

    async def save(self, source: ConfigSource) -> None:
        """Write config into the working tree and create a local Git commit.

        .. note::
            Only a local commit is created.  Pushing to the remote is **out of
            scope** — call :meth:`refresh` to pull and :meth:`save` to write,
            then push manually if needed.
        """
        git = _require_git()

        fs = await self._ensure_repo_async()
        await fs.save(source)

        repo: Any = self._repo

        def _commit() -> None:
            # Stage all modified / new files under the work tree.
            repo.git.add(A=True)
            if not repo.index.diff("HEAD") and not repo.untracked_files:
                _logger.debug("GitConfigBackend.save: nothing to commit")
                return
            commit_msg = f"pyfly: update {source.application}/{source.profile}@{source.label}"
            try:
                repo.index.commit(commit_msg)
            except git.GitCommandError as exc:
                _logger.warning("GitConfigBackend.save: commit failed: %s", exc)

        await self._run_sync(_commit)

    async def list(self) -> list[ConfigSource]:
        fs = await self._ensure_repo_async()
        return await fs.list()

    # ------------------------------------------------------------------
    # Git-specific extra
    # ------------------------------------------------------------------

    async def refresh(self) -> None:
        """Pull the latest commits from ``origin`` (no-op when no remote exists)."""
        if importlib.util.find_spec("git") is None:
            msg = "GitConfigBackend requires GitPython — `pip install pyfly[config-server-git]`"
            raise ImportError(msg)

        await self._ensure_repo_async()  # ensures self._repo is set
        repo: Any = self._repo
        if not repo.remotes:
            _logger.debug("GitConfigBackend.refresh: no remotes — skipping pull")
            return

        def _pull() -> None:
            repo.remotes.origin.pull()

        _logger.debug("GitConfigBackend.refresh: pulling from origin")
        await self._run_sync(_pull)
