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
"""PyFly Logging — hexagonal logging port and adapters."""

from __future__ import annotations

import logging
from typing import Any

from pyfly.logging.port import LoggingPort
from pyfly.logging.stdlib_adapter import StdlibLoggingAdapter

__all__ = ["LoggingPort", "StdlibLoggingAdapter", "get_logger"]


def get_logger(name: str) -> Any:
    """Return a structured logger that accepts ``logger.info(event, **kwargs)``.

    Uses ``structlog`` when installed, otherwise a stdlib-backed shim that
    renders ``event | key=value`` output. This keeps every call site
    structlog-style and safe even when the ``observability`` extra (which
    ships ``structlog``) is not installed — passing keyword fields to a raw
    stdlib :class:`logging.Logger` would otherwise raise ``TypeError`` on
    every call.
    """
    try:
        import structlog

        return structlog.get_logger(name)
    except ImportError:
        from pyfly.logging.stdlib_adapter import _StructuredLogger

        return _StructuredLogger(logging.getLogger(name))


try:
    from pyfly.logging.structlog_adapter import StructlogAdapter

    __all__ += ["StructlogAdapter"]
except ImportError:
    pass
