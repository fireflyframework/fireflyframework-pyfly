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
"""The config and file sources (spec 4.5, 6.2)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from pyfly.core.config import Config
from pyfly.feature_flags.definitions import FlagDefinitionError
from pyfly.feature_flags.sources import FlagSource, FlagSourceError, SourceSnapshot
from pyfly.feature_flags.sources.config import ConfigFlagSource
from pyfly.feature_flags.sources.file import FileFlagSource
from tests.feature_flags.support import bool_flag


async def test_the_config_source_reads_the_sections_verbatim_and_expands_the_shorthand() -> None:
    flags = {
        "new-checkout": True,
        "checkout_flow": "v2",
        "page-size": {"state": "ENABLED", "variants": {"s": 10}},
    }
    evaluators = {"is-beta": {"in": ["beta", {"var": "roles"}]}}
    config = Config({"pyfly": {"feature-flags": {"flags": flags, "evaluators": evaluators}}})
    source = ConfigFlagSource.from_config(config)
    snapshot = await source.load()
    assert isinstance(source, FlagSource)
    assert (source.name, source.fail_fast, source.refresh_interval) == ("config", True, None)
    assert set(snapshot.document.flags) == {"new-checkout", "checkout_flow", "page-size"}  # kebab and snake kept
    assert snapshot.document.flags["new-checkout"] == bool_flag("on")
    assert snapshot.document.evaluators == {"is-beta": {"in": ["beta", {"var": "roles"}]}}
    assert snapshot.revision is None


async def test_the_config_source_rejects_an_invalid_definition() -> None:
    source = ConfigFlagSource({"bad key": True})
    with pytest.raises(FlagDefinitionError, match="invalid flag key"):
        await source.load()


async def test_a_yaml_config_with_an_unquoted_expires_date_loads(tmp_path: Path) -> None:
    """Review focus 1: pyfly.yaml is YAML 1.1 (PyYAML): `expires: 2026-12-31` arrives as a datetime.date."""
    (tmp_path / "pyfly.yaml").write_text(
        "pyfly:\n  feature-flags:\n    flags:\n      promo:\n        state: ENABLED\n"
        '        variants: {"on": true, "off": false}\n        defaultVariant: "on"\n'
        "        metadata: {expires: 2026-12-31, owner: web}\n",
        encoding="utf-8",
    )
    snapshot = await ConfigFlagSource.from_config(Config.from_file(tmp_path / "pyfly.yaml")).load()
    assert snapshot.document.flags["promo"]["metadata"] == {"expires": "2026-12-31", "owner": "web"}


def _write(path: Path, flags: dict[str, object]) -> None:
    path.write_text(json.dumps({"flags": flags}), encoding="utf-8")


async def test_the_file_source_loads_json_and_reports_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "flags.json"
    _write(path, {"a": bool_flag()})
    source = FileFlagSource(path, refresh_interval=0.5)
    first = await source.load()
    assert isinstance(first, SourceSnapshot) and first.document.flags == {"a": bool_flag()}
    assert (source.name, source.fail_fast, source.refresh_interval) == ("file", True, 0.5)
    assert await source.load() is None  # same mtime and size


async def test_the_file_source_sees_a_rewrite(tmp_path: Path) -> None:
    path = tmp_path / "flags.json"
    _write(path, {"a": bool_flag("on")})
    source = FileFlagSource(path)
    await source.load()
    _write(path, {"a": bool_flag("off")})  # same size: the mtime tells it apart
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    changed = await source.load()
    assert changed is not None and changed.document.flags["a"]["defaultVariant"] == "off"


async def test_a_yaml_file_with_an_unquoted_expires_date_loads(tmp_path: Path) -> None:
    """Review focus 1, and flagd's own YAML reading: `on:`/`off:` stay names in a flag file (YAML 1.2 booleans)."""
    path = tmp_path / "flags.yaml"
    path.write_text(
        "flags:\n  promo:\n    state: ENABLED\n    variants: {on: true, off: false}\n    defaultVariant: on\n"
        "    metadata: {expires: 2026-12-31}\n",
        encoding="utf-8",
    )
    snapshot = await FileFlagSource(path).load()
    assert snapshot is not None
    assert snapshot.document.flags["promo"] == bool_flag("on", metadata={"expires": "2026-12-31"})


async def test_an_invalid_file_is_rejected_and_retried(tmp_path: Path) -> None:
    path = tmp_path / "flags.json"
    path.write_text(json.dumps({"flags": {"a": True}}), encoding="utf-8")  # shorthand: config and tests only
    source = FileFlagSource(path)
    with pytest.raises(FlagDefinitionError, match="flag definition must be an object"):
        await source.load()
    with pytest.raises(FlagDefinitionError):
        await source.load()  # not remembered as loaded: the next poll tries again


@pytest.mark.parametrize(
    ("name", "text"), [("flags.json", "[]"), ("flags.json", "0"), ("flags.yaml", "[]"), ("flags.yaml", "false")]
)
async def test_a_file_whose_top_level_is_falsy_but_not_an_object_is_rejected(
    tmp_path: Path, name: str, text: str
) -> None:
    """Only an absent document (empty or comment-only YAML, JSON null) is empty: `[]`, `0`, `false` are errors."""
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    with pytest.raises(FlagDefinitionError, match="flag definition must be an object"):
        await FileFlagSource(path).load()


@pytest.mark.parametrize(
    ("name", "text"), [("flags.json", "null"), ("flags.yaml", ""), ("flags.yml", "# no flags yet\n")]
)
async def test_an_absent_document_is_an_empty_one(tmp_path: Path, name: str, text: str) -> None:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    snapshot = await FileFlagSource(path).load()
    assert snapshot is not None and snapshot.document.flags == {} and snapshot.document.evaluators == {}


async def test_a_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        await FileFlagSource(tmp_path / "absent.json").load()


@pytest.mark.parametrize("name", ["flags.txt", "flags", "flags.toml"])
def test_only_json_and_yaml_files_are_sources(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError, match=r"\.json, \.yaml or \.yml"):
        FileFlagSource(tmp_path / name)


def test_a_source_error_names_the_source_and_the_cause() -> None:
    error = FlagSourceError("file", FileNotFoundError("flags.json"))
    assert error.source == "file" and str(error) == "feature flag source 'file' failed to load: flags.json"
