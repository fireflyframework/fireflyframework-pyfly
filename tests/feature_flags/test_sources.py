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
import threading
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


@pytest.mark.parametrize("value", [["new-checkout"], [], "new-checkout", "", True, False, 0, 1.5], ids=repr)
@pytest.mark.parametrize(
    ("section", "key", "reason"),
    [
        ("flags", "flags", "flags must be an object"),
        ("evaluators", "$evaluators", "$evaluators must be an object"),
    ],
)
async def test_a_config_section_that_is_not_a_map_fails_the_load(
    section: str, key: str, reason: str, value: object
) -> None:
    """`Config.get_section` answers `{}` for a list, a string, a boolean or a number: that must not load zero flags."""
    config = Config({"pyfly": {"feature-flags": {section: value}}})
    with pytest.raises(FlagDefinitionError) as error:
        await ConfigFlagSource.from_config(config).load()
    assert (error.value.key, error.value.reason) == (key, reason)


@pytest.mark.parametrize("raw", [{}, {"flags": None, "evaluators": None}, {"flags": {}, "evaluators": {}}])
async def test_an_absent_or_null_config_section_is_empty(raw: dict[str, object]) -> None:
    snapshot = await ConfigFlagSource.from_config(Config({"pyfly": {"feature-flags": raw}})).load()
    assert snapshot.document.flags == {} and snapshot.document.evaluators == {}


async def test_the_config_source_rejects_a_section_that_is_not_a_map_given_directly() -> None:
    with pytest.raises(FlagDefinitionError, match="flags must be an object"):
        await ConfigFlagSource(["new-checkout"]).load()


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
    def flag(default: str) -> dict[str, object]:  # "aa" and "bb" are as long as each other: the size cannot tell
        return {"state": "ENABLED", "variants": {"aa": True, "bb": False}, "defaultVariant": default}

    path = tmp_path / "flags.json"
    _write(path, {"a": flag("aa")})
    source = FileFlagSource(path)
    await source.load()
    before = path.stat()
    _write(path, {"a": flag("bb")})
    os.utime(
        path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000)
    )  # only the mtime differs from the first read
    after = path.stat()
    assert after.st_size == before.st_size and after.st_mtime_ns != before.st_mtime_ns
    changed = await source.load()
    assert changed is not None and changed.document.flags["a"]["defaultVariant"] == "bb"


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


async def test_the_file_is_read_off_the_event_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A stalled mount must not freeze the loop: the stat, the read and the parse run in a worker thread."""
    path = tmp_path / "flags.json"
    _write(path, {"a": bool_flag()})
    threads: list[int] = []
    read = FileFlagSource._read

    def spy(self: FileFlagSource) -> object:
        threads.append(threading.get_ident())
        return read(self)

    monkeypatch.setattr(FileFlagSource, "_read", spy)
    source = FileFlagSource(path)
    assert await source.load() is not None
    assert await source.load() is None
    assert len(threads) == 2 and threading.get_ident() not in threads


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("flags.json", b'{"flags": '),
        ("flags.yaml", b"flags: [unclosed\n"),
        ("flags.yml", b"flags:\n  a: {b\n"),
        ("flags.json", b"\xff\xfe not utf-8"),
    ],
)
async def test_a_syntax_error_names_the_file_and_is_retried(tmp_path: Path, name: str, content: bytes) -> None:
    path = tmp_path / name
    path.write_bytes(content)
    source = FileFlagSource(path)
    for _ in range(2):  # a failed load is not remembered
        with pytest.raises(ValueError) as error:
            await source.load()
        assert str(path) in str(error.value) and error.value.__cause__ is not None


@pytest.mark.parametrize("name", ["flags.txt", "flags", "flags.toml"])
def test_only_json_and_yaml_files_are_sources(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError, match=r"\.json, \.yaml or \.yml"):
        FileFlagSource(tmp_path / name)


def test_a_source_error_names_the_source_and_the_cause() -> None:
    error = FlagSourceError("file", FileNotFoundError("flags.json"))
    assert error.source == "file" and str(error) == "feature flag source 'file' failed to load: flags.json"
