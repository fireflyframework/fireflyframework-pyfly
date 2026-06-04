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
"""Regression tests for #9 — @shell_option(type=...) / choices are honored."""

from __future__ import annotations

from pyfly.shell.decorators import shell_argument, shell_option
from pyfly.shell.param_inference import infer_params


class TestShellOptionTypeOverride:
    def test_explicit_type_overrides_inference(self):
        @shell_option("--x", type=int)
        def cmd(x): ...  # unannotated -> would infer str without the override

        (p,) = infer_params(cmd)
        assert p.param_type is int
        assert p.is_option is True

    def test_type_unset_falls_back_to_inferred(self):
        @shell_option("--y", help="h")
        def cmd(y: str = "z"): ...

        (p,) = infer_params(cmd)
        assert p.param_type is str  # fallback to the signature-inferred type

    def test_choices_are_stored(self):
        @shell_option("--mode", choices=["a", "b"])
        def cmd(mode: str = "a"): ...

        (p,) = infer_params(cmd)
        assert p.choices == ["a", "b"]

    def test_argument_type_override(self):
        @shell_argument("a", type=int)
        def cmd(a): ...

        (p,) = infer_params(cmd)
        assert p.param_type is int
        assert p.is_option is False
