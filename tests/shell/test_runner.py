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
"""Tests for runner protocols and ApplicationArguments."""

from __future__ import annotations

from pyfly.shell.runner import ApplicationArguments, ApplicationRunner, CommandLineRunner

# ---------------------------------------------------------------------------
# ApplicationArguments.from_args
# ---------------------------------------------------------------------------


class TestFromArgs:
    def test_mixed_args(self):
        raw = ["serve", "--port=8080", "--verbose", "extra"]
        args = ApplicationArguments.from_args(raw)

        assert args.source_args == ["serve", "--port=8080", "--verbose", "extra"]
        assert args.option_args == ["--port=8080", "--verbose"]
        assert args.non_option_args == ["serve", "extra"]

    def test_empty_args(self):
        args = ApplicationArguments.from_args([])

        assert args.source_args == []
        assert args.option_args == []
        assert args.non_option_args == []

    def test_only_options(self):
        raw = ["--debug", "--level=3"]
        args = ApplicationArguments.from_args(raw)

        assert args.option_args == ["--debug", "--level=3"]
        assert args.non_option_args == []

    def test_only_non_options(self):
        raw = ["run", "server", "fast"]
        args = ApplicationArguments.from_args(raw)

        assert args.option_args == []
        assert args.non_option_args == ["run", "server", "fast"]

    def test_source_args_is_a_copy(self):
        raw = ["--flag"]
        args = ApplicationArguments.from_args(raw)
        raw.append("mutated")

        assert "mutated" not in args.source_args


# ---------------------------------------------------------------------------
# contains_option
# ---------------------------------------------------------------------------


class TestContainsOption:
    def test_flag_present(self):
        args = ApplicationArguments.from_args(["--verbose"])
        assert args.contains_option("verbose") is True

    def test_key_value_present(self):
        args = ApplicationArguments.from_args(["--port=8080"])
        assert args.contains_option("port") is True

    def test_absent(self):
        args = ApplicationArguments.from_args(["--verbose"])
        assert args.contains_option("debug") is False

    def test_partial_name_does_not_match(self):
        args = ApplicationArguments.from_args(["--verbose-mode"])
        assert args.contains_option("verbose") is False

    def test_empty_args(self):
        args = ApplicationArguments.from_args([])
        assert args.contains_option("anything") is False


# ---------------------------------------------------------------------------
# get_option_values
# ---------------------------------------------------------------------------


class TestGetOptionValues:
    def test_single_value(self):
        args = ApplicationArguments.from_args(["--port=8080"])
        assert args.get_option_values("port") == ["8080"]

    def test_multiple_values(self):
        args = ApplicationArguments.from_args(["--tag=alpha", "--tag=beta"])
        assert args.get_option_values("tag") == ["alpha", "beta"]

    def test_missing_option(self):
        args = ApplicationArguments.from_args(["--port=8080"])
        assert args.get_option_values("host") == []

    def test_flag_without_value(self):
        args = ApplicationArguments.from_args(["--verbose"])
        assert args.get_option_values("verbose") == []

    def test_value_with_equals_sign(self):
        args = ApplicationArguments.from_args(["--formula=a=b"])
        assert args.get_option_values("formula") == ["a=b"]


# ---------------------------------------------------------------------------
# CommandLineRunner protocol
# ---------------------------------------------------------------------------


class TestCommandLineRunnerProtocol:
    def test_conforming_class_is_instance(self):
        class MyRunner:
            async def run(self, args: list[str]) -> None:
                pass

        assert isinstance(MyRunner(), CommandLineRunner)

    def test_non_conforming_class_is_not_instance(self):
        class NotARunner:
            def execute(self) -> None:
                pass

        assert not isinstance(NotARunner(), CommandLineRunner)


# ---------------------------------------------------------------------------
# ApplicationRunner protocol
# ---------------------------------------------------------------------------


class TestApplicationRunnerProtocol:
    def test_conforming_class_is_instance(self):
        class MyRunner:
            async def run(self, args: ApplicationArguments) -> None:
                pass

        assert isinstance(MyRunner(), ApplicationRunner)

    def test_non_conforming_class_is_not_instance(self):
        class NotARunner:
            def execute(self) -> None:
                pass

        assert not isinstance(NotARunner(), ApplicationRunner)
