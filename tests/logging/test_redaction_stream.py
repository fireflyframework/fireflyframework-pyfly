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

import io

from pyfly.logging.redaction.engine import RegexRedactor
from pyfly.logging.redaction.stream import RedactingTextIO


def test_redacts_complete_lines():
    buf = io.StringIO()
    stream = RedactingTextIO(buf, RegexRedactor(["EMAIL"]))
    stream.write("hello jane@acme.io\n")
    assert buf.getvalue() == "hello <EMAIL>\n"


def test_buffers_until_newline():
    buf = io.StringIO()
    stream = RedactingTextIO(buf, RegexRedactor(["EMAIL"]))
    stream.write("partial jane@")
    assert buf.getvalue() == ""  # held until newline/flush
    stream.write("acme.io\n")
    assert buf.getvalue() == "partial <EMAIL>\n"


def test_flush_emits_remainder():
    buf = io.StringIO()
    stream = RedactingTextIO(buf, RegexRedactor(["EMAIL"]))
    stream.write("tail jane@acme.io")
    stream.flush()
    assert buf.getvalue() == "tail <EMAIL>"
