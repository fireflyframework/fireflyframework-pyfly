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
"""Regression tests for config relaxed-binding fixes.

#90 — env-only nested keys reach effective_section()/bind().
#92 — _raw_get / placeholder references use relaxed kebab/snake matching.
"""

from __future__ import annotations

from dataclasses import dataclass

from pyfly.core.config import Config, config_properties


# Module-level so get_type_hints can resolve it during bind().
@config_properties(prefix="pyfly.newkey")
@dataclass
class _NewKeyProps:
    val: str = "default"


# ---------------------------------------------------------------------------
# #92 — relaxed _raw_get + placeholder references
# ---------------------------------------------------------------------------


class TestRelaxedRawGet:
    def test_raw_get_kebab_resolves_snake_storage(self):
        cfg = Config({"my_prop": {"sub_key": "V"}})
        assert cfg._raw_get("my-prop.sub-key") == "V"
        assert cfg._raw_get("my_prop.sub_key") == "V"
        assert cfg.get("my-prop.sub-key") == "V"

    def test_placeholder_reference_relaxed(self):
        cfg = Config({"my_prop": {"sub_key": "V"}, "msg": "${my-prop.sub-key}"})
        assert cfg.get("msg") == "V"

    def test_placeholder_reference_relaxed_reverse(self):
        cfg = Config({"my-prop": {"sub-key": "W"}, "msg": "${my_prop.sub_key}"})
        assert cfg.get("msg") == "W"

    def test_exact_match_unchanged(self):
        cfg = Config({"a": {"b": 1}})
        assert cfg.get("a.b") == 1
        assert cfg.get("a.x") is None


# ---------------------------------------------------------------------------
# #90 — env-only keys reach effective_section / bind
# ---------------------------------------------------------------------------


class TestEnvOnlyInjection:
    def test_env_only_nested_key_in_effective_section(self, monkeypatch):
        monkeypatch.setenv("PYFLY_NEWKEY_VAL", "x")
        cfg = Config({"pyfly": {"app": {"name": "svc"}}})
        section = cfg.effective_section("pyfly")
        assert section["newkey"] == {"val": "x"}

    def test_env_only_field_binds(self, monkeypatch):
        monkeypatch.setenv("PYFLY_NEWKEY_VAL", "fromenv")
        cfg = Config({})
        bound = cfg.bind(_NewKeyProps)
        assert bound.val == "fromenv"

    def test_env_overlay_does_not_clobber_existing_leaf(self, monkeypatch):
        monkeypatch.setenv("PYFLY_APP_NAME", "envname")
        cfg = Config({"pyfly": {"app": {"name": "filename"}}})
        section = cfg.effective_section("pyfly")
        assert section["app"]["name"] == "envname"  # first-pass overlay, not double-injected

    def test_no_env_leaves_section_unchanged(self):
        cfg = Config({"pyfly": {"app": {"name": "svc"}}})
        assert cfg.effective_section("pyfly") == {"app": {"name": "svc"}}

    def test_default_used_when_no_env(self):
        bound = Config({}).bind(_NewKeyProps)
        assert bound.val == "default"
