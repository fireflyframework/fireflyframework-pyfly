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
"""Tests for :class:`MessagingAutoConfiguration` — provider routing."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from pyfly.messaging.auto_configuration import MessagingAutoConfiguration


@pytest.mark.parametrize(
    ("available_modules", "expected_provider"),
    [
        # aiokafka installed → kafka (highest precedence).
        ({"aiokafka"}, "kafka"),
        # aio_pika without aiokafka → rabbitmq.
        ({"aio_pika"}, "rabbitmq"),
        # Neither installed → memory.
        (set(), "memory"),
        # Both installed → kafka wins.
        ({"aiokafka", "aio_pika"}, "kafka"),
    ],
)
def test_detect_provider_parametrized(
    available_modules: set[str],
    expected_provider: str,
) -> None:
    """detect_provider() returns the right messaging provider for each installed-module combination."""
    with patch("pyfly.config.auto.AutoConfiguration.is_available") as is_avail:
        is_avail.side_effect = lambda mod: mod in available_modules
        assert MessagingAutoConfiguration.detect_provider() == expected_provider
