# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Abrupt owned-process death at native EDA admission/effect boundaries."""

import asyncio
import json
import os
import sys
from pathlib import Path

from sqlalchemy.ext.asyncio import create_async_engine

from pyfly.audit import AuditActor, AuditContext, AuditRecorder, AuditResource, AuditScope
from pyfly.audit.sql import SqlAuditJournal
from pyfly.data.transaction import resolve_manager
from pyfly.eda.outbox import OutboxRelay, SqlOutboxStore


async def main():
    settings = json.loads(Path(sys.argv[1]).read_text())
    engine = create_async_engine(settings["url"])
    journal = SqlAuditJournal(engine, create_tables=False)
    await journal.start()

    class Resolver:
        async def resolve(self, operation):
            return AuditContext(
                scope=AuditScope(tenant_id="fixture", workspace_id="workspace", project_id="project"),
                actor=AuditActor(kind="service", subject_id="verified-fixture"),
                resource=AuditResource(kind="delivery", resource_id=operation.resource_key),
            )

    recorder = AuditRecorder(
        journal, Resolver(), transaction_manager=resolve_manager(engine), modules=frozenset({"eda"})
    )
    if settings["mode"] == "before_intent":

        async def before_intent(_):
            os._exit(71)

        journal.admit_once = before_intent
    elif settings["mode"] == "after_intent":
        independent = recorder._independent

        async def after_intent(event, **kwargs):
            result = await independent(event, **kwargs)
            if event.outcome == "intent":
                os._exit(72)
            return result

        recorder._independent = after_intent

    async def handler(_):
        fd = os.open(settings["marker"], os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, b"effect\n")
            os.fsync(fd)
        finally:
            os.close(fd)
        os._exit(73)

    handler.__module__, handler.__qualname__ = "owned_eda_fixture", "handler"
    store = SqlOutboxStore(engine, create_tables=False)
    relay = OutboxRelay(store, group="worker", register=False, transactional=False)
    relay._bind_audit_recorder(lambda: recorder)
    relay.subscribe("fixture.changed", handler)
    await relay.run_once()
    raise RuntimeError("Owned process did not reach its crash boundary")


if __name__ == "__main__":
    asyncio.run(main())
