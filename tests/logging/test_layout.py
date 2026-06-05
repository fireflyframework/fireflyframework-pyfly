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

from pyfly.logging.layout import compile_pattern


def test_basic_tokens():
    fmt, datefmt = compile_pattern("%d{%H:%M:%S} %p %c - %m")
    assert fmt == "%(asctime)s %(levelname)s %(name)s - %(message)s"
    assert datefmt == "%H:%M:%S"


def test_aliases_and_truncation():
    fmt, datefmt = compile_pattern("%level %logger{10} %msg%n")
    assert fmt == "%(levelname)s %(name)s %(message)s\n"
    assert datefmt is None


def test_unknown_tokens_pass_through():
    fmt, _ = compile_pattern("LIT %m DONE")
    assert fmt == "LIT %(message)s DONE"


def test_bare_date_token_no_braces():
    """Bare ``%d`` (without a ``{format}`` suffix) maps to ``%(asctime)s`` with datefmt=None."""
    fmt, datefmt = compile_pattern("%d %p - %m")
    assert fmt == "%(asctime)s %(levelname)s - %(message)s"
    assert datefmt is None
