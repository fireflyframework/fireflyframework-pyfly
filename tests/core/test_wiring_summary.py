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
"""The startup ``wiring_summary`` log must surface every decorator-wiring count.

Regression: EDA ``@event_listener`` subscriptions (``event_listeners_eda``) were
omitted from the summary, so it reported wired EDA listeners as absent.
"""

from __future__ import annotations

from pyfly.core.application import _wiring_summary_fields

_ADVERTISED = (
    "event_listeners",
    "event_listeners_eda",
    "message_listeners",
    "cqrs_handlers",
    "scheduled_tasks",
    "async_methods",
    "post_processors",
)


def test_summary_surfaces_eda_event_listeners() -> None:
    fields = _wiring_summary_fields({"event_listeners_eda": 3})
    assert fields["event_listeners_eda"] == 3


def test_summary_maps_scheduled_alias() -> None:
    # the raw counter key is "scheduled"; the summary exposes it as "scheduled_tasks"
    assert _wiring_summary_fields({"scheduled": 2})["scheduled_tasks"] == 2


def test_summary_includes_all_advertised_counts_defaulting_to_zero() -> None:
    fields = _wiring_summary_fields({})
    for key in _ADVERTISED:
        assert fields[key] == 0
