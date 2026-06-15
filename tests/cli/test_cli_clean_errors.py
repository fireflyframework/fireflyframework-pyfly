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
"""The PyFly CLI surfaces failures as clean one-line errors, not raw tracebacks."""

from __future__ import annotations

import click
from click.testing import CliRunner

from pyfly.cli.main import PyFlyCLI


def _group() -> click.Group:
    @click.group(cls=PyFlyCLI)
    @click.option("--debug", is_flag=True, envvar="PYFLY_DEBUG")
    def cli(debug: bool) -> None:  # noqa: ARG001
        pass

    @cli.command()
    def boom() -> None:
        raise ValueError("Configuration validation failed for 'ServerProperties'")

    return cli


def _all_output(result: object) -> str:
    out = getattr(result, "output", "") or ""
    try:
        err = result.stderr or ""  # type: ignore[attr-defined]
    except (ValueError, AttributeError):
        err = ""
    return out + err


def test_error_is_clean_no_traceback() -> None:
    result = CliRunner().invoke(_group(), ["boom"])
    combined = _all_output(result)
    assert result.exit_code == 1
    assert "Error:" in combined
    assert "Configuration validation failed" in combined
    # No raw Python traceback in the default output.
    assert "Traceback (most recent call last)" not in combined
    assert "raise ValueError(" not in combined


def test_debug_flag_shows_traceback() -> None:
    result = CliRunner().invoke(_group(), ["--debug", "boom"])
    combined = _all_output(result)
    assert result.exit_code == 1
    # Rich renders the traceback frames in debug mode.
    assert "ValueError" in combined
