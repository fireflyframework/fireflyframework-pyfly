# Copyright 2026 Firefly Software Foundation
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

"""pyfly Code Mode: expose native tools to sandboxed scripts.

Instead of the model emitting one tool call per step, it can emit a *script*
that orchestrates several tools at once, executed in the sandbox with those
tools registered as ``external_functions``. This cuts model round-trips while
keeping every host call audited and capability-gated.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable, Iterable
from typing import Any, cast

logger = logging.getLogger(__name__)


def _iter_tools(source: Any) -> Iterable[Any]:
    """Yield tool-like objects from a ToolKit, a list, or any iterable."""
    if hasattr(source, "tools"):
        candidate: Any = source.tools
        return cast("Iterable[Any]", candidate() if callable(candidate) else candidate)
    if hasattr(source, "list_tools"):
        return cast("Iterable[Any]", source.list_tools())
    if isinstance(source, Iterable):
        return source
    raise TypeError(f"cannot extract tools from {type(source).__name__!r}")


def _wrap_tool(tool: Any) -> Callable[..., Any]:
    """Adapt a native tool into an external function callable from guest code.

    Positional arguments from the script are mapped onto the tool's declared
    parameter names or the decorated handler's inferred signature. Execution
    still passes through ``execute`` for guards and call listeners.
    """
    raw_names = [getattr(p, "name", None) for p in (getattr(tool, "parameters", None) or [])]
    param_names = [p for p in raw_names if isinstance(p, str) and p]
    handler_factory = getattr(tool, "pydantic_handler", None)
    signature = inspect.signature(handler_factory()) if not param_names and callable(handler_factory) else None
    if signature is not None and getattr(tool, "takes_ctx", False):
        signature = signature.replace(parameters=[p for p in signature.parameters.values() if p.name != "ctx"])

    async def _call(*args: Any, **kwargs: Any) -> Any:
        if signature is not None:
            arguments = signature.bind(*args, **kwargs).arguments
            bound = {}
            for name, value in arguments.items():
                kind = signature.parameters[name].kind
                if kind == inspect.Parameter.VAR_KEYWORD:
                    bound.update(value)
                elif kind == inspect.Parameter.VAR_POSITIONAL:
                    if value:
                        raise TypeError("Code Mode tools require named parameters instead of *args")
                else:
                    bound[name] = value
        else:
            if len(args) > len(param_names):
                raise TypeError(f"Tool {tool.name!r} accepts at most {len(param_names)} positional arguments")
            bound = dict(zip(param_names, args, strict=False))
            if bound.keys() & kwargs.keys():
                raise TypeError("Tool arguments cannot be provided both positionally and by keyword")
            bound.update(kwargs)
        result = tool.execute(**bound)
        if inspect.isawaitable(result):
            result = await result
        return result

    _call.__name__ = str(getattr(tool, "name", "tool"))
    return _call


def toolkit_external_functions(source: Any, *, names: Iterable[str] | None = None) -> dict[str, Callable[..., Any]]:
    """Build an ``external_functions`` mapping from native tools.

    Args:
        source: A ``ToolKit``, an object exposing ``tools``/``list_tools()``, or
            an iterable of tool-like objects.
        names: Optional allowlist of tool names to expose (defaults to all).

    Returns:
        A ``{tool_name: callable}`` dict ready to pass to
        :meth:`ExecutionEnvironment.run_code` / :meth:`SecureScriptRunner.execute`.
    """
    allow = set(names) if names is not None else None
    funcs: dict[str, Callable[..., Any]] = {}
    for tool in _iter_tools(source):
        name = getattr(tool, "name", None)
        if not name or (allow is not None and name not in allow):
            continue
        funcs[name] = _wrap_tool(tool)
    logger.debug("Code Mode exposed %d tool(s) as external functions", len(funcs))
    return funcs
