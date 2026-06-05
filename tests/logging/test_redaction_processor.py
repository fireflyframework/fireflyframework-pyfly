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

import logging

from pyfly.logging.redaction.engine import RegexRedactor
from pyfly.logging.redaction.processor import RedactionFilter, make_structlog_redactor


def test_structlog_processor_redacts_event_and_fields():
    r = RegexRedactor(["EMAIL"])
    proc = make_structlog_redactor(r, allow_fields=[], deny_fields=["password"])
    out = proc(None, "info", {"event": "login jane@acme.io", "user": "bob@x.io", "password": "hunter2"})
    assert out["event"] == "login <EMAIL>"
    assert out["user"] == "<EMAIL>"
    assert out["password"] == "<REDACTED>"


def test_structlog_allow_fields_limits_scanning():
    r = RegexRedactor(["EMAIL"])
    proc = make_structlog_redactor(r, allow_fields=["event"], deny_fields=[])
    out = proc(None, "info", {"event": "a jane@acme.io", "note": "keep bob@x.io"})
    assert out["event"] == "a <EMAIL>"
    assert out["note"] == "keep bob@x.io"  # not in allow list -> untouched


def test_stdlib_filter_redacts_message():
    r = RegexRedactor(["EMAIL"])
    flt = RedactionFilter(r, allow_fields=[], deny_fields=[])
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, "mail %s", ("jane@acme.io",), None)
    assert flt.filter(rec) is True
    assert rec.getMessage() == "mail <EMAIL>"
