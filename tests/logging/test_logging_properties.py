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
from __future__ import annotations

from pyfly.config.properties.logging import LoggingProperties
from pyfly.core.config import Config


def test_defaults():
    props = Config({}).bind(LoggingProperties)
    assert props.level == {"root": "INFO"}
    assert props.format == "console"
    assert props.redaction.enabled is True
    assert props.redaction.engine == "auto"
    assert "EMAIL" in props.redaction.entities
    assert props.redaction.deny_fields == ["password", "token", "secret"]
    assert props.redaction.streams.enabled is False
    assert props.file.name == ""


def test_relaxed_nested_kebab_keys_bind():
    cfg = Config(
        {
            "pyfly": {
                "logging": {
                    "format": "json",
                    "file": {"name": "app.log", "path": "./logs"},
                    "rolling": {"max-size": "10MB", "max-history": 5},
                    "pattern": {"console": "%d %p %c - %m"},
                    "redaction": {
                        "engine": "regex",
                        "mask": "partial",
                        "entities": ["EMAIL"],
                        "extra-patterns": {"EMP": "EMP-\\d+"},
                        "deny-fields": ["pw"],
                        "streams": {"enabled": True},
                        "presidio": {"score-threshold": 0.7, "languages": ["en", "es"]},
                    },
                }
            }
        }
    )
    props = cfg.bind(LoggingProperties)
    assert props.format == "json"
    assert props.file.name == "app.log"
    assert props.rolling.max_size == "10MB"
    assert props.rolling.max_history == 5
    assert props.pattern.console == "%d %p %c - %m"
    assert props.redaction.engine == "regex"
    assert props.redaction.mask == "partial"
    assert props.redaction.entities == ["EMAIL"]
    assert props.redaction.extra_patterns == {"EMP": "EMP-\\d+"}
    assert props.redaction.deny_fields == ["pw"]
    assert props.redaction.streams.enabled is True
    assert props.redaction.presidio.score_threshold == 0.7
    assert props.redaction.presidio.languages == ["en", "es"]
