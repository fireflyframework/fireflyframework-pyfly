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
"""StructlogAdapter — default LoggingPort with unified formatting + PII redaction."""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from typing import Any, cast

import structlog

from pyfly.config.properties.logging import LoggingProperties
from pyfly.core.config import Config
from pyfly.logging.config_loader import apply_external_config
from pyfly.logging.handlers import build_file_handler
from pyfly.logging.redaction.engine import build_redactor
from pyfly.logging.redaction.processor import make_structlog_redactor
from pyfly.logging.redaction.stream import install_stream_redaction


class StructlogAdapter:
    """Default logging adapter backed by structlog with one formatter for all records."""

    def __init__(self) -> None:
        self._restore_streams: Callable[[], None] | None = None

    def configure(self, config: Config) -> None:
        props = config.bind(LoggingProperties)
        redactor = build_redactor(props.redaction)
        fmt = props.format.lower()

        # Shared pre-chain applied to BOTH structlog and foreign (stdlib) records.
        timestamper = structlog.processors.TimeStamper(fmt="iso")
        shared_pre: list[structlog.types.Processor] = [
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            timestamper,
        ]
        if redactor is not None:
            shared_pre.append(
                cast(
                    "structlog.types.Processor",
                    make_structlog_redactor(redactor, props.redaction.allow_fields, props.redaction.deny_fields),
                )
            )

        renderer: structlog.types.Processor = (
            structlog.processors.JSONRenderer()
            if fmt == "json"
            else structlog.processors.KeyValueRenderer(key_order=["timestamp", "level", "logger", "event"])
            if fmt == "logfmt"
            else structlog.dev.ConsoleRenderer(colors=False, sort_keys=False)
        )

        # structlog -> stdlib bridge: ProcessorFormatter renders the final record.
        structlog.configure(
            processors=[*shared_pre, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
            cache_logger_on_first_use=True,
        )
        formatter = structlog.stdlib.ProcessorFormatter(
            processor=renderer,
            foreign_pre_chain=shared_pre,
        )

        root = logging.getLogger()
        root.setLevel(getattr(logging, str(props.level.get("root", "INFO")).upper(), logging.INFO))
        for handler in list(root.handlers):
            root.removeHandler(handler)

        if props.config and apply_external_config(props.config):
            handlers = list(root.handlers)
            for handler in handlers:
                handler.setFormatter(formatter)
        else:
            console = logging.StreamHandler(stream=sys.stdout)
            console.setFormatter(formatter)
            handlers = [console]
            file_handler = build_file_handler(props.file, props.rolling)
            if file_handler is not None:
                file_handler.setFormatter(formatter)
                handlers.append(file_handler)
            for handler in handlers:
                root.addHandler(handler)

        # NOTE: redaction is handled entirely by ``make_structlog_redactor`` in
        # ``shared_pre`` — which runs for structlog records (it's in the configured
        # processor chain) AND for foreign stdlib records (it's the
        # ``foreign_pre_chain``). We deliberately do NOT add a handler-level
        # RedactionFilter here: at filter time a structlog record's ``msg`` is the
        # wrapped event dict, so redacting its ``getMessage()`` would corrupt the
        # value ProcessorFormatter expects.

        for name, level in props.level.items():
            if name != "root":
                self.set_level(name, str(level))

        if self._restore_streams is not None:
            self._restore_streams()
            self._restore_streams = None
        if redactor is not None and props.redaction.streams.enabled:
            self._restore_streams = install_stream_redaction(redactor)

    def get_logger(self, name: str) -> Any:
        return structlog.get_logger(name)

    def set_level(self, name: str, level: str) -> None:
        log_level = getattr(logging, level.upper(), logging.INFO)
        logging.getLogger(name).setLevel(log_level)
