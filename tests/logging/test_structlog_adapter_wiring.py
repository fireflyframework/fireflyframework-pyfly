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
"""Wiring tests for StructlogAdapter — ProcessorFormatter + foreign_pre_chain + redaction."""

from __future__ import annotations

import logging

import pytest

structlog = pytest.importorskip("structlog")

from pyfly.core.config import Config  # noqa: E402
from pyfly.logging.structlog_adapter import StructlogAdapter  # noqa: E402


def _reset_root() -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)


def test_foreign_logger_formatted_and_redacted(capsys):
    _reset_root()
    adapter = StructlogAdapter()
    adapter.configure(Config({"pyfly": {"logging": {"format": "console"}}}))
    # A plain stdlib (foreign) logger — must be rendered through the unified
    # formatter AND redacted.
    logging.getLogger("sqlalchemy.engine").warning("connect user=jane@acme.io")
    cap = capsys.readouterr()
    out = cap.out + cap.err
    assert "<EMAIL>" in out
    assert "jane@acme.io" not in out
    assert "warning" in out.lower()  # level rendered (unified format), not bare message


def test_structlog_event_redacted(capsys):
    _reset_root()
    adapter = StructlogAdapter()
    adapter.configure(Config({"pyfly": {"logging": {"format": "console"}}}))
    adapter.get_logger("app").info("login", email="jane@acme.io")
    cap = capsys.readouterr()
    out = cap.out + cap.err
    assert "<EMAIL>" in out
    assert "jane@acme.io" not in out
