# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Explicit archive composition; signing authority is always supplied by the application."""

import math

from pyfly.audit.archive import AuditArchiveDelivery
from pyfly.audit.archive_models import MAX_ARCHIVE_BYTES
from pyfly.audit.archive_ports import ArchiveError, ArchiveReceiptStore, ArchiveSink
from pyfly.audit.local_archive import LocalArchiveAdapter
from pyfly.audit.ports import AuditJournal, CheckpointSigner, CheckpointVerifier
from pyfly.container.bean import bean
from pyfly.container.container import Container
from pyfly.context.conditions import auto_configuration, conditional_on_missing_bean, conditional_on_property
from pyfly.core.config import Config
from pyfly.data.relational.framework_schema import context_datasource_registry, creates_tables, module_datasource
from pyfly.eda.outbox import SqlOutboxStore


@auto_configuration
@conditional_on_property("pyfly.audit.archive.enabled", having_value="true")
@conditional_on_missing_bean(AuditArchiveDelivery)
class AuditArchiveAutoConfiguration:
    @bean
    @conditional_on_property("pyfly.audit.archive.adapter", having_value="local")
    @conditional_on_missing_bean(ArchiveSink)
    def local_audit_archive(self, config: Config) -> LocalArchiveAdapter:
        path = config.get("pyfly.audit.archive.path")
        sink_id = config.get("pyfly.audit.archive.sink-id")
        if not isinstance(path, str) or not path or not isinstance(sink_id, str) or not sink_id:
            raise ArchiveError("Explicit local archive directory and sink identity are required")
        return LocalArchiveAdapter(
            path, sink_id=sink_id, max_bytes=config.get("pyfly.audit.archive.max-bytes", MAX_ARCHIVE_BYTES)
        )

    @bean
    @conditional_on_property("pyfly.audit.archive.adapter", having_value="local")
    @conditional_on_missing_bean(ArchiveSink)
    def archive_sink(self, backend: LocalArchiveAdapter) -> ArchiveSink:
        return backend

    @bean
    @conditional_on_property("pyfly.audit.archive.adapter", having_value="local")
    @conditional_on_missing_bean(ArchiveReceiptStore)
    def archive_receipt_store(self, backend: LocalArchiveAdapter) -> ArchiveReceiptStore:
        return backend

    @bean
    def audit_archive_delivery(
        self,
        config: Config,
        journal: AuditJournal,
        sink: ArchiveSink,
        receipt_store: ArchiveReceiptStore,
        signer: CheckpointSigner,
        verifier: CheckpointVerifier,
        container: Container | None = None,
    ) -> AuditArchiveDelivery:
        registry = context_datasource_registry(config, container)
        datasource = module_datasource(registry, config, "pyfly.audit", name="audit")
        interval = config.get("pyfly.audit.archive.poll-interval-seconds", 5.0)
        if type(interval) not in (int, float) or not math.isfinite(interval) or interval <= 0:
            raise ArchiveError("Archive poll interval must be a finite positive number")
        return AuditArchiveDelivery(
            journal,
            SqlOutboxStore(datasource, create_tables=creates_tables(registry.properties.ddl_auto)),
            sink,
            receipt_store,
            signer,
            verifier,
            journal_id=str(config.get("pyfly.audit.journal-id", "default")),
            max_bytes=config.get("pyfly.audit.archive.max-bytes", MAX_ARCHIVE_BYTES),
            poll_interval=float(interval),
        )
