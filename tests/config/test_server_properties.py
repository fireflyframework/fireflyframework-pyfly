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
"""Tests for ServerProperties configuration dataclass."""

from __future__ import annotations

from pyfly.config.properties.server import (
    GranianProperties,
    ServerObservabilityProperties,
    ServerProperties,
)


class TestServerProperties:
    def test_default_values(self):
        props = ServerProperties()
        assert props.type == "auto"
        assert props.event_loop == "auto"
        assert props.workers == 1
        assert props.backlog == 1024
        assert props.graceful_timeout == 30
        assert props.http == "auto"
        assert props.ssl_certfile is None
        assert props.ssl_keyfile is None
        assert props.keep_alive_timeout == 5
        assert props.max_concurrent_connections is None
        assert props.max_requests_per_worker is None

    def test_granian_defaults(self):
        props = ServerProperties()
        assert props.granian.runtime_threads == 1
        assert props.granian.runtime_mode == "auto"
        assert props.granian.backpressure is None
        assert props.granian.respawn_failed_workers is True

    def test_custom_values(self):
        props = ServerProperties(
            type="granian",
            workers=4,
            http="2",
            granian=GranianProperties(runtime_threads=2),
        )
        assert props.type == "granian"
        assert props.workers == 4
        assert props.http == "2"
        assert props.granian.runtime_threads == 2

    def test_has_config_properties_prefix(self):
        assert hasattr(ServerProperties, "__pyfly_config_prefix__")
        assert ServerProperties.__pyfly_config_prefix__ == "pyfly.server"

    def test_observability_defaults(self):
        props = ServerProperties()
        assert props.observability.enabled is True
        assert props.observability.sample_interval_seconds == 5.0
        assert props.observability.access_log is False

    def test_observability_custom_values(self):
        props = ServerProperties(
            observability=ServerObservabilityProperties(enabled=False, sample_interval_seconds=2.0, access_log=True)
        )
        assert props.observability.enabled is False
        assert props.observability.sample_interval_seconds == 2.0
        assert props.observability.access_log is True


class TestServerObservabilityProperties:
    def test_defaults(self):
        obs = ServerObservabilityProperties()
        assert obs.enabled is True
        assert obs.sample_interval_seconds == 5.0
        assert obs.access_log is False
