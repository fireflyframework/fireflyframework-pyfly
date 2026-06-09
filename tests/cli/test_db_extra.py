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
"""Tests for the extended 'pyfly db' subcommands (alembic mocked)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from pyfly.cli.main import cli


@pytest.fixture
def fake_alembic(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    command = MagicMock()
    monkeypatch.setattr("pyfly.cli.db._require_alembic", lambda: (command, object))
    monkeypatch.setattr("pyfly.cli.db._get_alembic_config", lambda: "CFG")
    return command


class TestDbExtra:
    def test_current(self, fake_alembic: MagicMock) -> None:
        result = CliRunner().invoke(cli, ["db", "current"])
        assert result.exit_code == 0, result.output
        fake_alembic.current.assert_called_once()

    def test_history(self, fake_alembic: MagicMock) -> None:
        result = CliRunner().invoke(cli, ["db", "history"])
        assert result.exit_code == 0, result.output
        fake_alembic.history.assert_called_once()

    def test_heads(self, fake_alembic: MagicMock) -> None:
        result = CliRunner().invoke(cli, ["db", "heads"])
        assert result.exit_code == 0, result.output
        fake_alembic.heads.assert_called_once()

    def test_show(self, fake_alembic: MagicMock) -> None:
        result = CliRunner().invoke(cli, ["db", "show", "abc123"])
        assert result.exit_code == 0, result.output
        fake_alembic.show.assert_called_once_with("CFG", "abc123")

    def test_revision_empty_by_default(self, fake_alembic: MagicMock) -> None:
        result = CliRunner().invoke(cli, ["db", "revision", "-m", "manual"])
        assert result.exit_code == 0, result.output
        _, kwargs = fake_alembic.revision.call_args
        assert kwargs["autogenerate"] is False
        assert kwargs["message"] == "manual"

    def test_revision_autogenerate(self, fake_alembic: MagicMock) -> None:
        result = CliRunner().invoke(cli, ["db", "revision", "-m", "auto", "--autogenerate"])
        assert result.exit_code == 0, result.output
        _, kwargs = fake_alembic.revision.call_args
        assert kwargs["autogenerate"] is True

    def test_stamp(self, fake_alembic: MagicMock) -> None:
        result = CliRunner().invoke(cli, ["db", "stamp", "head"])
        assert result.exit_code == 0, result.output
        fake_alembic.stamp.assert_called_once_with("CFG", "head")

    def test_merge(self, fake_alembic: MagicMock) -> None:
        result = CliRunner().invoke(cli, ["db", "merge", "rev1", "rev2", "-m", "merge"])
        assert result.exit_code == 0, result.output
        args, kwargs = fake_alembic.merge.call_args
        assert args[0] == "CFG"
        assert list(args[1]) == ["rev1", "rev2"]
        assert kwargs["message"] == "merge"

    def test_reset_requires_confirmation(self, fake_alembic: MagicMock) -> None:
        CliRunner().invoke(cli, ["db", "reset"], input="n\n")
        assert fake_alembic.downgrade.call_count == 0

    def test_reset_yes(self, fake_alembic: MagicMock) -> None:
        result = CliRunner().invoke(cli, ["db", "reset", "--yes"])
        assert result.exit_code == 0, result.output
        fake_alembic.downgrade.assert_called_once_with("CFG", "base")
        fake_alembic.upgrade.assert_called_once_with("CFG", "head")
