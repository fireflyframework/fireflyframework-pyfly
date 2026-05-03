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
"""Tests for Environment with profile support."""

from pyfly.context.environment import Environment
from pyfly.core.config import Config


class TestEnvironment:
    def test_default_no_profiles(self):
        env = Environment(Config({}))
        assert env.active_profiles == []

    def test_profiles_from_config(self):
        env = Environment(Config({"pyfly": {"profiles": {"active": "production,staging"}}}))
        assert env.active_profiles == ["production", "staging"]

    def test_profiles_from_env_var(self, monkeypatch):
        monkeypatch.setenv("PYFLY_PROFILES_ACTIVE", "test,ci")
        env = Environment(Config({}))
        assert env.active_profiles == ["test", "ci"]

    def test_env_var_overrides_config(self, monkeypatch):
        monkeypatch.setenv("PYFLY_PROFILES_ACTIVE", "override")
        env = Environment(Config({"pyfly": {"profiles": {"active": "original"}}}))
        assert env.active_profiles == ["override"]

    def test_accepts_profiles_match(self):
        env = Environment(Config({"pyfly": {"profiles": {"active": "production"}}}))
        assert env.accepts_profiles("production") is True
        assert env.accepts_profiles("staging") is False
        assert env.accepts_profiles("production", "staging") is True

    def test_get_property_from_config(self):
        env = Environment(Config({"app": {"name": "MyApp"}}))
        assert env.get_property("app.name") == "MyApp"

    def test_get_property_default(self):
        env = Environment(Config({}))
        assert env.get_property("missing.key", "default") == "default"

    def test_get_property_none_when_missing(self):
        env = Environment(Config({}))
        assert env.get_property("missing.key") is None


class TestAcceptsProfilesNegation:
    def test_negation_matches_when_profile_not_active(self):
        env = Environment(Config({}))
        env._active_profiles = ["dev"]
        assert env.accepts_profiles("!production") is True

    def test_negation_does_not_match_when_profile_active(self):
        env = Environment(Config({}))
        env._active_profiles = ["production"]
        assert env.accepts_profiles("!production") is False

    def test_negation_mixed_with_positive(self):
        env = Environment(Config({}))
        env._active_profiles = ["dev"]
        assert env.accepts_profiles("dev", "!production") is True

    def test_negation_no_profiles_active(self):
        env = Environment(Config({}))
        env._active_profiles = []
        assert env.accepts_profiles("!production") is True


class TestAcceptsMultiProfile:
    def test_comma_separated_matches_first(self):
        env = Environment(Config({}))
        env._active_profiles = ["dev"]
        assert env.accepts_profiles("dev,test") is True

    def test_comma_separated_matches_second(self):
        env = Environment(Config({}))
        env._active_profiles = ["test"]
        assert env.accepts_profiles("dev,test") is True

    def test_comma_separated_no_match(self):
        env = Environment(Config({}))
        env._active_profiles = ["production"]
        assert env.accepts_profiles("dev,test") is False
