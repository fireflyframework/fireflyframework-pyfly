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
"""Tests for the structlog-or-stdlib ``get_logger`` factory."""

from __future__ import annotations

import builtins
import importlib


def test_get_logger_accepts_structured_kwargs():
    from pyfly.logging import get_logger

    log = get_logger("pyfly.test")
    # Must not raise regardless of which backend is active.
    log.info("an_event", method="GET", path="/x", status_code=200)
    log.error("an_error", error="boom")


def test_get_logger_falls_back_when_structlog_missing(monkeypatch):
    """When structlog is not importable, get_logger must still return a logger
    that accepts ``event, **kwargs`` (stdlib loggers would raise TypeError)."""
    real_import = builtins.__import__

    def _no_structlog(name, *args, **kwargs):
        if name == "structlog" or name.startswith("structlog."):
            raise ImportError("structlog disabled for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_structlog)

    import pyfly.logging as logging_mod

    importlib.reload(logging_mod)
    log = logging_mod.get_logger("pyfly.test.fallback")
    # The stdlib-backed shim accepts structlog-style calls without raising.
    log.info("http_request", method="GET", path="/widgets", status_code=200)

    # Restore the module to its structlog-enabled state for other tests.
    monkeypatch.undo()
    importlib.reload(logging_mod)
