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
"""Tests for shell stereotypes and method/parameter decorators."""

from pyfly.container.stereotypes import shell_component
from pyfly.container.types import Scope
from pyfly.shell.decorators import shell_argument, shell_method, shell_option

# ---------------------------------------------------------------------------
# @shell_component stereotype
# ---------------------------------------------------------------------------


class TestShellComponentStereotype:
    def test_marks_class_injectable(self):
        @shell_component
        class MyCmds:
            pass

        assert MyCmds.__pyfly_injectable__ is True
        assert MyCmds.__pyfly_stereotype__ == "shell_component"

    def test_with_explicit_name(self):
        @shell_component(name="x")
        class MyCmds:
            pass

        assert MyCmds.__pyfly_bean_name__ == "x"

    def test_preserves_class_identity(self):
        @shell_component
        class MyCmds:
            """My shell commands."""

            def do_stuff(self):
                return 42

        assert MyCmds.__name__ == "MyCmds"
        assert MyCmds.__doc__ == "My shell commands."
        assert MyCmds().do_stuff() == 42

    def test_default_scope_is_singleton(self):
        @shell_component
        class MyCmds:
            pass

        assert MyCmds.__pyfly_scope__ == Scope.SINGLETON


# ---------------------------------------------------------------------------
# @shell_method
# ---------------------------------------------------------------------------


class TestShellMethod:
    def test_bare_marks_method(self):
        @shell_method()
        def say_hello():
            pass

        assert say_hello.__pyfly_shell_method__ is True

    def test_infers_key_from_name(self):
        @shell_method()
        def say_hello():
            pass

        assert say_hello.__pyfly_shell_key__ == "say-hello"

    def test_explicit_metadata(self):
        @shell_method(key="say-hello", help="Says hello", group="greetings")
        def greet():
            pass

        assert greet.__pyfly_shell_key__ == "say-hello"
        assert greet.__pyfly_shell_help__ == "Says hello"
        assert greet.__pyfly_shell_group__ == "greetings"

    def test_preserves_function_behavior(self):
        @shell_method()
        def add(a: int, b: int) -> int:
            """Add two numbers."""
            return a + b

        assert add(1, 2) == 3
        assert add.__doc__ == "Add two numbers."


# ---------------------------------------------------------------------------
# @shell_option
# ---------------------------------------------------------------------------


class TestShellOption:
    def test_stores_option_metadata(self):
        @shell_option("--verbose", is_flag=True, help="Enable verbose mode")
        @shell_method()
        def my_cmd():
            pass

        assert len(my_cmd.__pyfly_shell_options__) == 1
        opt = my_cmd.__pyfly_shell_options__[0]
        assert opt["name"] == "--verbose"
        assert opt["is_flag"] is True
        assert opt["help"] == "Enable verbose mode"


# ---------------------------------------------------------------------------
# @shell_argument
# ---------------------------------------------------------------------------


class TestShellArgument:
    def test_stores_argument_metadata(self):
        @shell_argument("name", help="Your name")
        @shell_method()
        def greet():
            pass

        assert len(greet.__pyfly_shell_arguments__) == 1
        arg = greet.__pyfly_shell_arguments__[0]
        assert arg["name"] == "name"
        assert arg["help"] == "Your name"
