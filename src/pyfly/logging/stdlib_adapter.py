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
"""StdlibLoggingAdapter — zero-(hard-)dependency LoggingPort with unified formatting + redaction."""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from typing import Any

from pyfly.config.properties.logging import LoggingProperties
from pyfly.core.config import Config
from pyfly.logging.config_loader import apply_external_config
from pyfly.logging.handlers import build_file_handler
from pyfly.logging.layout import compile_pattern
from pyfly.logging.redaction.engine import build_redactor
from pyfly.logging.redaction.processor import RedactionFilter
from pyfly.logging.redaction.stream import install_stream_redaction


class _StructuredLogger:
    """Wraps stdlib Logger to accept structlog-style calls: logger.info(event, **kwargs)."""

    __slots__ = ("_logger",)

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def _format(self, event: str, kwargs: dict[str, Any]) -> str:
        if kwargs:
            pairs = " ".join(f"{k}={v}" for k, v in kwargs.items())
            return f"{event} | {pairs}"
        return event

    def debug(self, event: str, **kwargs: Any) -> None:
        self._logger.debug(self._format(event, kwargs))

    def info(self, event: str, **kwargs: Any) -> None:
        self._logger.info(self._format(event, kwargs))

    def warning(self, event: str, **kwargs: Any) -> None:
        self._logger.warning(self._format(event, kwargs))

    def error(self, event: str, **kwargs: Any) -> None:
        self._logger.error(self._format(event, kwargs))

    def critical(self, event: str, **kwargs: Any) -> None:
        self._logger.critical(self._format(event, kwargs))

    def exception(self, event: str, **kwargs: Any) -> None:
        self._logger.exception(self._format(event, kwargs))


_JSON_FMT = '{"timestamp":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","message":"%(message)s"}'
_DEFAULT_CONSOLE = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


class StdlibLoggingAdapter:
    """Fallback LoggingPort using only stdlib logging — now with unified formatting + redaction."""

    def __init__(self) -> None:
        self._restore_streams: Callable[[], None] | None = None

    def configure(self, config: Config) -> None:
        props = config.bind(LoggingProperties)
        redactor = build_redactor(props.redaction)

        root = logging.getLogger()
        root_level = getattr(logging, str(props.level.get("root", "INFO")).upper(), logging.INFO)
        root.setLevel(root_level)
        for handler in list(root.handlers):
            root.removeHandler(handler)

        if props.config and apply_external_config(props.config):
            handlers = list(root.handlers)
        else:
            handlers = self._build_handlers(props)
            for handler in handlers:
                root.addHandler(handler)

        if redactor is not None:
            redaction_filter = RedactionFilter(redactor, props.redaction.allow_fields, props.redaction.deny_fields)
            for handler in handlers:
                handler.addFilter(redaction_filter)

        for name, level in props.level.items():
            if name != "root":
                self.set_level(name, str(level))

        if self._restore_streams is not None:
            self._restore_streams()
            self._restore_streams = None
        if redactor is not None and props.redaction.streams.enabled:
            self._restore_streams = install_stream_redaction(redactor)

    def _build_handlers(self, props: LoggingProperties) -> list[logging.Handler]:
        if props.format == "json":
            console_fmt, datefmt = _JSON_FMT, None
        elif props.pattern.console:
            console_fmt, datefmt = compile_pattern(props.pattern.console)
        else:
            console_fmt, datefmt = _DEFAULT_CONSOLE, None

        console = logging.StreamHandler(stream=sys.stdout)
        console.setFormatter(logging.Formatter(console_fmt, datefmt=datefmt))
        handlers: list[logging.Handler] = [console]

        file_handler = build_file_handler(props.file, props.rolling)
        if file_handler is not None:
            if props.pattern.file:
                file_fmt, file_datefmt = compile_pattern(props.pattern.file)
            elif props.format == "json":
                file_fmt, file_datefmt = _JSON_FMT, None
            else:
                file_fmt, file_datefmt = _DEFAULT_CONSOLE, None
            file_handler.setFormatter(logging.Formatter(file_fmt, datefmt=file_datefmt))
            handlers.append(file_handler)
        return handlers

    def get_logger(self, name: str) -> Any:
        return _StructuredLogger(logging.getLogger(name))

    def set_level(self, name: str, level: str) -> None:
        log_level = getattr(logging, level.upper(), logging.INFO)
        logging.getLogger(name).setLevel(log_level)
