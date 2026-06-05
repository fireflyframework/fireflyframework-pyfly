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
"""Shell method and parameter decorators for CLI command definitions.

Mirrors Spring Shell's @ShellMethod, @ShellOption, and @ShellArgument annotations.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

F = TypeVar("F", bound=Callable[..., Any])


def shell_method(
    key: str = "",
    *,
    help: str = "",
    group: str = "",
) -> Callable[[F], F]:
    """Mark a method as a shell command.

    Parameters
    ----------
    key:
        The command name (kebab-case). Defaults to the function name with
        underscores replaced by hyphens.
    help:
        Help text displayed in the shell's ``help`` command.
    group:
        Logical command group for organised help output.
    """

    def decorator(func: F) -> F:
        func.__pyfly_shell_method__ = True  # type: ignore[attr-defined]
        func.__pyfly_shell_key__ = key or func.__name__.replace("_", "-")  # type: ignore[attr-defined]
        func.__pyfly_shell_help__ = help  # type: ignore[attr-defined]
        func.__pyfly_shell_group__ = group  # type: ignore[attr-defined]
        return func

    return decorator


def shell_option(
    name: str,
    *,
    type: type | None = None,
    is_flag: bool = False,
    help: str = "",
    default: Any = None,
    choices: list[str] | None = None,
) -> Callable[[F], F]:
    """Attach option metadata to a shell command method.

    Parameters
    ----------
    name:
        The option name (e.g. ``--verbose``).
    type:
        Expected value type. ``None`` means infer from the function signature.
    is_flag:
        If ``True`` the option is a boolean flag (no value expected).
    help:
        Help text for this option.
    default:
        Default value when the option is not supplied.
    choices:
        Allowed values; surfaced on the resolved :class:`ShellParam`.
    """

    def decorator(func: F) -> F:
        options: list[dict[str, Any]] = getattr(func, "__pyfly_shell_options__", [])
        options.append(
            {
                "name": name,
                "type": type,
                "is_flag": is_flag,
                "help": help,
                "default": default,
                "choices": choices,
            }
        )
        func.__pyfly_shell_options__ = options  # type: ignore[attr-defined]
        return func

    return decorator


def shell_method_availability(
    checker: str,
) -> Callable[[F], F]:
    """Link a shell command to an availability checker method.

    The *checker* is the name of a method on the same ``@shell_component``
    class that returns ``""`` (available) or a reason string (unavailable).
    When unavailable, the command is hidden from help and blocked from execution.

    Usage::

        @shell_component
        class AdminCommands:
            def __init__(self, security: SecurityContext):
                self._security = security

            def admin_available(self) -> str:
                if self._security.has_role("ADMIN"):
                    return ""
                return "Requires ADMIN role"

            @shell_method(key="reset-db", help="Reset the database")
            @shell_method_availability("admin_available")
            async def reset_db(self) -> str:
                return "Database reset!"
    """

    def decorator(func: F) -> F:
        func.__pyfly_shell_availability__ = checker  # type: ignore[attr-defined]
        return func

    return decorator


def shell_argument(
    name: str,
    *,
    type: type | None = None,
    help: str = "",
    required: bool = True,
    default: Any = None,
) -> Callable[[F], F]:
    """Attach positional argument metadata to a shell command method.

    Parameters
    ----------
    name:
        The argument name.
    type:
        Expected value type. ``None`` means infer from the function signature.
    help:
        Help text for this argument.
    required:
        Whether the argument must be supplied.
    default:
        Default value when the argument is not supplied.
    """

    def decorator(func: F) -> F:
        arguments: list[dict[str, Any]] = getattr(func, "__pyfly_shell_arguments__", [])
        arguments.append(
            {
                "name": name,
                "type": type,
                "help": help,
                "required": required,
                "default": default,
            }
        )
        func.__pyfly_shell_arguments__ = arguments  # type: ignore[attr-defined]
        return func

    return decorator
