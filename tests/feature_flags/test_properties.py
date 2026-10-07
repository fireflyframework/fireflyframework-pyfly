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
"""FeatureFlagsProperties: binding, defaults and validation (spec 8)."""

from __future__ import annotations

from typing import Any

import pytest

from pyfly.core.config import Config
from pyfly.feature_flags.properties import FeatureFlagsProperties


def _bind(section: dict[str, Any]) -> FeatureFlagsProperties:
    return FeatureFlagsProperties.from_config(Config({"pyfly": {"feature-flags": section}}))


def test_the_defaults_are_the_spec_defaults() -> None:
    props = _bind({})
    assert props.enabled is False and props.openfeature.domain == ""
    assert (props.sources.file.enabled, props.sources.file.path, props.sources.file.refresh_interval) == (
        False,
        "",
        "5s",
    )
    http = props.sources.http
    assert (http.enabled, http.url, http.token, http.refresh_interval, http.timeout) == (False, "", "", "30s", "2s")
    store = props.sources.store
    assert (store.enabled, store.driver, store.refresh_interval, store.datasource) == (False, "database", "5s", "")
    assert (props.context.tenant_attribute, props.context.trust_tenant_header) == ("tenant", False)
    assert props.web.disabled_status == 404 and props.events.evaluations is False and props.management.writes is False
    server = props.server
    assert (server.enabled, server.path, server.token, server.allow_anonymous) == (
        False,
        "/feature-flags/flagd.json",
        "",
        False,
    )


def test_kebab_case_keys_and_string_values_bind() -> None:
    props = _bind(
        {
            "enabled": "true",
            "sources": {"file": {"enabled": "true", "path": "flags.yaml", "refresh-interval": "500ms"}},
            "context": {"tenant-attribute": "org", "trust-tenant-header": "true"},
            "web": {"disabled-status": "503"},
            "server": {"enabled": True, "allow-anonymous": True},
        }
    )
    assert props.enabled is True and props.sources.file.path == "flags.yaml"
    assert props.seconds(props.sources.file.refresh_interval, "sources.file.refresh-interval") == 0.5
    assert props.context.tenant_attribute == "org" and props.context.trust_tenant_header is True
    assert props.web.disabled_status == 503 and props.server.allow_anonymous is True


@pytest.mark.parametrize(
    ("section", "message"),
    [
        ({"web": {"disabled-status": 410}}, "web.disabled-status must be 404, 403 or 503"),
        ({"sources": {"store": {"driver": "redis"}}}, "sources.store.driver must be database or memory"),
        ({"sources": {"file": {"enabled": True}}}, "sources.file.path is required"),
        ({"sources": {"file": {"enabled": True, "path": "flags.txt"}}}, r"\.json, \.yaml or \.yml"),
        ({"sources": {"http": {"enabled": True}}}, "sources.http.url is required"),
        ({"sources": {"http": {"enabled": True, "url": "http://cp", "timeout": "soon"}}}, "sources.http.timeout"),
        ({"sources": {"store": {"enabled": True, "refresh-interval": "0s"}}}, "must be greater than 0"),
        ({"server": {"enabled": True}}, "server.token"),
        ({"server": {"enabled": True, "token": "t", "path": "flags.json"}}, "server.path must start with /"),
        ({"server": {"enabled": True, "token": "t", "path": "/"}}, "server.path must not be the root"),
        ({"server": {"enabled": True, "token": "t", "path": "/foo/../"}}, "server.path must not be the root"),
    ],
)
def test_invalid_settings_fail_with_the_key(section: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _bind(section)


# R-refresh-zero: 0 would silently mean "never poll" (the registry polls only a positive interval).
_ENABLED_SOURCES: dict[str, dict[str, Any]] = {
    "file": {"enabled": True, "path": "flags.yaml"},
    "http": {"enabled": True, "url": "http://control-plane/flags.json"},
    "store": {"enabled": True},
}


@pytest.mark.parametrize("source", sorted(_ENABLED_SOURCES))
@pytest.mark.parametrize("interval", [0, 0.0, "0", "0s", "0ms", -5])
def test_a_polling_source_refuses_a_refresh_interval_that_is_not_positive(source: str, interval: Any) -> None:
    section = {"sources": {source: {**_ENABLED_SOURCES[source], "refresh-interval": interval}}}
    key = rf"pyfly\.feature-flags\.sources\.{source}\.refresh-interval"
    with pytest.raises(ValueError, match=rf"^{key} must be greater than 0, got {interval!r}$"):
        _bind(section)


@pytest.mark.parametrize("source", sorted(_ENABLED_SOURCES))
@pytest.mark.parametrize("interval", ["-1s", "soon", float("inf"), float("nan"), True])
def test_a_refresh_interval_that_is_not_a_duration_names_the_key(source: str, interval: Any) -> None:
    section = {"sources": {source: {**_ENABLED_SOURCES[source], "refresh-interval": interval}}}
    key = rf"pyfly\.feature-flags\.sources\.{source}\.refresh-interval"
    with pytest.raises(ValueError, match=rf"^{key} must be a duration such as '5s' or '500ms'"):
        _bind(section)


@pytest.mark.parametrize("source", sorted(_ENABLED_SOURCES))
def test_a_positive_refresh_interval_is_accepted(source: str) -> None:
    props = _bind({"sources": {source: {**_ENABLED_SOURCES[source], "refresh-interval": "250ms"}}})
    interval = getattr(props.sources, source).refresh_interval
    assert props.seconds(interval, f"sources.{source}.refresh-interval") == 0.25


def test_a_disabled_source_is_not_validated() -> None:
    """Only an enabled source's settings are used, so only an enabled source's settings are checked."""
    props = _bind({"sources": {"file": {"refresh-interval": 0}, "http": {"timeout": "soon"}}})
    assert props.sources.file.enabled is False and props.sources.http.enabled is False


def test_an_unconvertible_value_names_the_prefix() -> None:
    with pytest.raises(ValueError, match=r"^pyfly\.feature-flags: .*'not-a-status'"):
        _bind({"web": {"disabled-status": "not-a-status"}})


def test_placeholders_in_the_flag_maps_are_left_alone() -> None:
    """The ``flags``/``evaluators`` maps are flag text, read verbatim by the config source: binding never resolves
    them, so ``${name}`` in a variant is not a missing configuration key."""
    section = {"flags": {"greeting": "Hello ${name}"}, "evaluators": {"note": {"in": ["${", {"var": "text"}]}}}
    config = Config({"pyfly": {"feature-flags": section}})
    assert FeatureFlagsProperties.from_config(config) == FeatureFlagsProperties()
    assert config.get_section("pyfly.feature-flags.flags") == {"greeting": "Hello ${name}"}  # the config is untouched


def test_placeholders_and_environment_overrides_still_apply_to_the_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLAGS_FILE", "/etc/shop/flags.yaml")
    monkeypatch.setenv("PYFLY_FEATURE_FLAGS_WEB_DISABLED_STATUS", "503")
    config = Config(
        {
            "shop": {"domain": "payments"},
            "pyfly": {
                "feature-flags": {
                    "flags": {"greeting": "Hello ${name}"},
                    "openfeature": {"domain": "${shop.domain}"},
                    "sources": {"file": {"enabled": True, "path": "${FLAGS_FILE}"}},
                    "web": {"disabled-status": 404},  # the leaf pyfly-defaults.yaml provides, which the env overrides
                }
            },
        }
    )
    props = FeatureFlagsProperties.from_config(config)
    assert props.sources.file.path == "/etc/shop/flags.yaml"  # an environment variable
    assert props.openfeature.domain == "payments"  # another configuration key
    assert props.web.disabled_status == 503  # PYFLY_* override


def test_an_unresolvable_placeholder_in_a_setting_still_fails() -> None:
    with pytest.raises(ValueError, match=r"^pyfly\.feature-flags: Cannot resolve placeholder '\$\{NO_SUCH_VAR\}'"):
        _bind({"sources": {"file": {"enabled": True, "path": "${NO_SUCH_VAR}"}}})


def test_tokens_are_masked_in_configuration_views() -> None:
    config = Config({})
    assert config.mask_value("pyfly.feature-flags.server.token", "s3cret") == "******"
    assert config.mask_value("pyfly.feature-flags.sources.http.token", "s3cret") == "******"


def test_the_framework_defaults_carry_every_key() -> None:
    defaults = Config._load_framework_defaults()["pyfly"]["feature-flags"]
    assert defaults["enabled"] is False and defaults["flags"] == {} and defaults["evaluators"] == {}
    assert defaults["sources"]["http"]["timeout"] == "2s"
    assert defaults["server"]["path"] == "/feature-flags/flagd.json"
    assert defaults["web"]["disabled-status"] == 404


def test_the_framework_defaults_bind_to_the_property_defaults() -> None:
    """Every key of the ``feature-flags`` defaults block binds, and to the value the dataclasses default to."""
    defaults = Config._load_framework_defaults()["pyfly"]["feature-flags"]
    bound = FeatureFlagsProperties.from_config(Config({"pyfly": {"feature-flags": defaults}}))
    assert bound == FeatureFlagsProperties()
