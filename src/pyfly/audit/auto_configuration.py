# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Opt-in native audit bean; use the business datasource so writes commit atomically."""

from typing import Any, cast

from pyfly.audit.ports import AuditJournal
from pyfly.audit.sql import SqlAuditJournal
from pyfly.container.bean import bean
from pyfly.container.container import Container
from pyfly.context.conditions import auto_configuration, conditional_on_missing_bean, conditional_on_property
from pyfly.core.config import Config
from pyfly.data.relational.framework_schema import context_datasource_registry, creates_tables, module_datasource


@auto_configuration
@conditional_on_property("pyfly.audit.enabled", having_value="true")
@conditional_on_missing_bean(AuditJournal)
class AuditAutoConfiguration:
    @bean
    def audit_journal(self, config: Config, container: Container | None = None) -> AuditJournal:
        registry = context_datasource_registry(config, container)
        datasource = module_datasource(registry, config, "pyfly.audit", name="audit")
        tracer = meter = None
        if container is not None:
            try:
                from opentelemetry.metrics import MeterProvider
                from opentelemetry.trace import TracerProvider

                if container.contains_type(TracerProvider):
                    tracer = container.resolve(cast(type[Any], TracerProvider))
                if container.contains_type(MeterProvider):
                    meter = container.resolve(cast(type[Any], MeterProvider))
            except ImportError:
                pass
        return SqlAuditJournal(
            datasource,
            journal_id=str(config.get("pyfly.audit.journal-id", "default")),
            create_tables=creates_tables(registry.properties.ddl_auto),
            tracer_provider=tracer,
            meter_provider=meter,
        )
