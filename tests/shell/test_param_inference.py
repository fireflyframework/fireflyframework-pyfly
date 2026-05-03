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
"""Tests for shell parameter inference from type hints."""

from __future__ import annotations

from pyfly.shell.decorators import shell_argument, shell_method, shell_option
from pyfly.shell.param_inference import infer_params
from pyfly.shell.result import MISSING, ShellParam

# ---------------------------------------------------------------------------
# Individual parameter kinds
# ---------------------------------------------------------------------------


class TestStrArgument:
    """A bare ``str`` param with no default is a positional argument."""

    def test_str_param_is_argument(self):
        def greet(name: str) -> None: ...

        (p,) = infer_params(greet)
        assert p.name == "name"
        assert p.param_type is str
        assert p.is_option is False
        assert p.default is MISSING


class TestIntArgument:
    """A bare ``int`` param with no default is a positional argument."""

    def test_int_param_is_argument(self):
        def repeat(count: int) -> None: ...

        (p,) = infer_params(repeat)
        assert p.name == "count"
        assert p.param_type is int
        assert p.is_option is False
        assert p.default is MISSING


class TestBoolFlag:
    """A ``bool`` param with a default becomes a flag option."""

    def test_bool_with_default_is_flag(self):
        def run(verbose: bool = False) -> None: ...

        (p,) = infer_params(run)
        assert p.name == "verbose"
        assert p.param_type is bool
        assert p.is_option is True
        assert p.is_flag is True
        assert p.default is False


class TestOptionWithDefault:
    """A param with a non-None default becomes an option."""

    def test_param_with_default_is_option(self):
        def fetch(retries: int = 3) -> None: ...

        (p,) = infer_params(fetch)
        assert p.name == "retries"
        assert p.param_type is int
        assert p.is_option is True
        assert p.default == 3


class TestOptionalNone:
    """``str | None = None`` is recognised as an optional option."""

    def test_pep604_optional_is_option(self):
        def search(query: str | None = None) -> None: ...

        (p,) = infer_params(search)
        assert p.name == "query"
        assert p.param_type is str
        assert p.is_option is True
        assert p.default is None


# ---------------------------------------------------------------------------
# Skipping ``self`` and ``return``
# ---------------------------------------------------------------------------


class TestSkipSelfAndReturn:
    """Parameters named ``self`` and the return annotation are excluded."""

    def test_class_method_skips_self(self):
        class Greeter:
            def hello(self, name: str) -> str: ...

        params = infer_params(Greeter.hello)
        names = [p.name for p in params]
        assert "self" not in names
        assert "return" not in names
        assert len(params) == 1
        assert params[0].name == "name"


# ---------------------------------------------------------------------------
# Composite / integration
# ---------------------------------------------------------------------------


class TestMultipleParams:
    """Multiple params of different kinds are inferred correctly."""

    def test_mixed_params(self):
        def cmd(name: str, count: int = 1, verbose: bool = False) -> None: ...

        params = infer_params(cmd)
        assert len(params) == 3

        name_p, count_p, verbose_p = params

        # name → positional argument
        assert name_p == ShellParam(name="name", param_type=str, is_option=False, default=MISSING)

        # count → option with default
        assert count_p == ShellParam(name="count", param_type=int, is_option=True, default=1)

        # verbose → flag option
        assert verbose_p == ShellParam(
            name="verbose",
            param_type=bool,
            is_option=True,
            default=False,
            is_flag=True,
        )


# ---------------------------------------------------------------------------
# Explicit decorator overrides
# ---------------------------------------------------------------------------


class TestShellOptionOverride:
    """@shell_option metadata overrides inferred param."""

    def test_option_override_with_help_and_flag(self):
        @shell_method()
        @shell_option("--verbose", is_flag=True, help="Enable verbose output")
        def deploy(verbose: bool = False) -> str:
            return "deployed"

        (p,) = infer_params(deploy)
        assert p.name == "verbose"
        assert p.is_option is True
        assert p.is_flag is True
        assert p.help_text == "Enable verbose output"


class TestShellArgumentOverride:
    """@shell_argument metadata overrides inferred param."""

    def test_argument_override_with_help(self):
        @shell_method()
        @shell_argument("service", help="Service to deploy")
        def deploy(service: str) -> str:
            return service

        (p,) = infer_params(deploy)
        assert p.name == "service"
        assert p.is_option is False
        assert p.help_text == "Service to deploy"
