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
"""Infer :class:`ShellParam` descriptors from a callable's type hints."""

from __future__ import annotations

import inspect
import types
import typing
from collections.abc import Callable
from typing import Any, Union, get_type_hints

from pyfly.shell.result import MISSING, ShellParam

_SKIP = frozenset({"self", "return"})


def _unwrap_optional(tp: Any) -> tuple[Any, bool]:
    """Unwrap ``Optional[T]``, ``Union[T, None]``, and ``T | None`` (PEP 604).

    Returns a ``(inner_type, was_optional)`` tuple.  If *tp* is not an
    optional wrapper the original type is returned unchanged.

    Handles nested generics like ``Optional[List[str]]`` and PEP 604
    unions like ``List[str] | None``.
    """
    # Both ``types.UnionType`` (PEP 604) and ``typing.Union`` are handled
    # uniformly via get_origin/get_args.
    origin = typing.get_origin(tp)

    if origin is Union or isinstance(tp, types.UnionType):
        args = typing.get_args(tp)
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1 and type(None) in args:
            return non_none[0], True
        return tp, False

    return tp, False


def infer_params(func: Callable[..., Any]) -> list[ShellParam]:
    """Build a list of :class:`ShellParam` from *func*'s signature and type hints.

    Explicit ``@shell_option`` / ``@shell_argument`` decorator metadata is
    merged on top of the inferred defaults when present.
    """
    sig = inspect.signature(func)
    try:
        hints = get_type_hints(func)
    except Exception:  # pragma: no cover – defensive for unresolvable hints
        hints = {}

    # Build lookup tables from explicit @shell_option / @shell_argument metadata
    option_overrides = _build_override_map(getattr(func, "__pyfly_shell_options__", []))
    argument_overrides = _build_override_map(getattr(func, "__pyfly_shell_arguments__", []))

    params: list[ShellParam] = []

    for name, p in sig.parameters.items():
        if name in _SKIP:
            continue

        raw_type = hints.get(name, str)
        inner_type, was_optional = _unwrap_optional(raw_type)
        has_default = p.default is not inspect.Parameter.empty

        # Determine default value
        if has_default:
            default: Any = p.default
        elif was_optional:
            default = None
        else:
            default = MISSING

        # Check for explicit overrides
        opt_meta = option_overrides.get(name)
        arg_meta = argument_overrides.get(name)

        if opt_meta is not None:
            # Explicit @shell_option override — honor an explicit type= (audit #9),
            # falling back to the signature-inferred inner_type when unset.
            params.append(
                ShellParam(
                    name=name,
                    param_type=opt_meta.get("type") or inner_type,
                    is_option=True,
                    default=opt_meta.get("default", default),
                    help_text=opt_meta.get("help", ""),
                    is_flag=opt_meta.get("is_flag", False),
                    choices=opt_meta.get("choices"),
                )
            )
            continue

        if arg_meta is not None:
            # Explicit @shell_argument override — honor an explicit type= (audit #9).
            params.append(
                ShellParam(
                    name=name,
                    param_type=arg_meta.get("type") or inner_type,
                    is_option=False,
                    default=arg_meta.get("default", default),
                    help_text=arg_meta.get("help", ""),
                )
            )
            continue

        # bool → flag option
        if inner_type is bool:
            params.append(
                ShellParam(
                    name=name,
                    param_type=bool,
                    is_option=True,
                    default=default if default is not MISSING else False,
                    is_flag=True,
                )
            )
            continue

        # Has a default or is optional → option
        is_option = has_default or was_optional

        params.append(
            ShellParam(
                name=name,
                param_type=inner_type,
                is_option=is_option,
                default=default,
            )
        )

    return params


def _build_override_map(metadata_list: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Build a {param_name: metadata} lookup from decorator metadata.

    Option names like ``--env`` are normalised to ``env``.
    """
    result: dict[str, dict[str, Any]] = {}
    for entry in metadata_list:
        raw_name: str = entry.get("name", "")
        # Normalise --kebab-case to snake_case param name
        clean = raw_name.lstrip("-").replace("-", "_")
        if clean:
            result[clean] = entry
    return result
