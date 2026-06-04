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
"""Regression tests for #187 — MessageFormat-compatible i18n substitution."""

from __future__ import annotations

from pyfly.i18n.adapters.resource_bundle import ResourceBundleMessageSource as MessageSource


class TestMessageFormatSubstitution:
    def test_plain_placeholder(self):
        assert MessageSource._substitute("hi {0}", ("world",)) == "hi world"

    def test_multiple_placeholders(self):
        assert MessageSource._substitute("{0}-{1}", ("a", "b")) == "a-b"

    def test_doubled_quote_is_literal_apostrophe(self):
        # Java MessageFormat: '' renders as a single ' (audit #187).
        assert MessageSource._substitute("It''s {0}", ("here",)) == "It's here"

    def test_single_quoted_text_is_literal(self):
        # '{0}' is a quoted literal and must NOT be substituted.
        assert MessageSource._substitute("'{0}' is literal, {0} is not", ("X",)) == "{0} is literal, X is not"

    def test_missing_argument_left_as_placeholder(self):
        assert MessageSource._substitute("cost {1}", ("only-one",)) == "cost {1}"

    def test_format_type_inserts_positional_arg(self):
        # {0,number} parses the index and inserts the arg (style not locale-applied).
        assert MessageSource._substitute("n={0,number}", (5,)) == "n=5"

    def test_unmatched_brace_left_intact(self):
        assert MessageSource._substitute("a { b", ()) == "a { b"

    def test_no_args_no_placeholders(self):
        assert MessageSource._substitute("plain text", ()) == "plain text"
