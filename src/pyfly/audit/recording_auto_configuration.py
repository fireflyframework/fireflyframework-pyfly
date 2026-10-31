# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Explicit native hook enrollment; trusted actor and project resolution belongs to the host."""

from pyfly.audit.ports import AuditError, AuditJournal
from pyfly.audit.recording import AuditRecorder
from pyfly.audit.recording_binding import AUTOMATIC_AUDIT_MODULES, AuditModulePostProcessor
from pyfly.audit.recording_context import AuditContextResolver
from pyfly.container.bean import bean
from pyfly.container.container import Container
from pyfly.container.provider import Provider
from pyfly.context.conditions import auto_configuration, conditional_on_missing_bean, conditional_on_property
from pyfly.core.config import Config
from pyfly.data.relational.framework_schema import context_datasource_registry, module_datasource
from pyfly.data.transaction import resolve_manager


@auto_configuration
@conditional_on_property("pyfly.audit.recording.enabled", having_value="true")
class AuditRecordingAutoConfiguration:
    @bean
    @conditional_on_missing_bean(AuditRecorder)
    def audit_recorder(
        self, config: Config, journal: AuditJournal, resolver: AuditContextResolver, container: Container
    ) -> AuditRecorder:
        modules = config.get("pyfly.audit.recording.modules")
        if (
            not isinstance(modules, list)
            or not modules
            or any(not isinstance(module, str) or module not in AUTOMATIC_AUDIT_MODULES for module in modules)
        ):
            raise AuditError("Configure explicit supported automatic audit modules: agentic, data, eda, security, web")
        registry = context_datasource_registry(config, container)
        datasource = module_datasource(registry, config, "pyfly.audit", name="audit")
        return AuditRecorder(
            journal,
            resolver,
            transaction_manager=resolve_manager(datasource),
            modules=frozenset(modules),
        )

    @bean
    def audit_module_post_processor(self, recorder: Provider[AuditRecorder]) -> AuditModulePostProcessor:
        return AuditModulePostProcessor(recorder.get)
