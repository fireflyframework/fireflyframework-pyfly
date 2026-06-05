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
"""Integration tests for StdlibLoggingAdapter — unified formatting + redaction + file output."""

from __future__ import annotations

import logging
import pathlib

from pyfly.core.config import Config
from pyfly.logging.stdlib_adapter import StdlibLoggingAdapter


def _reset_root() -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)


def test_third_party_logger_is_redacted_and_formatted(capsys):
    _reset_root()
    adapter = StdlibLoggingAdapter()
    adapter.configure(Config({"pyfly": {"logging": {"format": "console"}}}))
    logging.getLogger("some.thirdparty").warning("user jane@acme.io logged in")
    err = capsys.readouterr()
    out = err.out + err.err
    assert "<EMAIL>" in out
    assert "jane@acme.io" not in out


def test_file_output(tmp_path: pathlib.Path):
    _reset_root()
    adapter = StdlibLoggingAdapter()
    adapter.configure(
        Config(
            {
                "pyfly": {
                    "logging": {
                        "file": {"name": "app.log", "path": str(tmp_path)},
                        "redaction": {"enabled": False},
                    }
                }
            }
        )
    )
    logging.getLogger("x").error("boom")
    for h in logging.getLogger().handlers:
        h.flush()
    assert "boom" in (tmp_path / "app.log").read_text()


def test_redaction_disabled_keeps_raw(capsys):
    _reset_root()
    adapter = StdlibLoggingAdapter()
    adapter.configure(Config({"pyfly": {"logging": {"redaction": {"enabled": False}}}}))
    logging.getLogger("x").warning("mail jane@acme.io")
    cap = capsys.readouterr()
    assert "jane@acme.io" in (cap.out + cap.err)
