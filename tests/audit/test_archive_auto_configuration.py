# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Explicit archive DI and lifecycle never manufacture signing authority."""

import asyncio

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pyfly.audit import AuditJournal, CheckpointSigner, CheckpointVerifier
from pyfly.audit.auto_configuration import AuditAutoConfiguration
from pyfly.container.exceptions import NoSuchBeanError
from pyfly.context import ApplicationContext
from pyfly.core import Config
from pyfly.data.transaction import infrastructure_unit
from tests.audit.test_archive_contracts import SCOPE, feature
from tests.integration.test_audit_journal import event


def configuration(tmp_path, *, enabled=True):
    return Config(
        {
            "pyfly": {
                "audit": {
                    "enabled": True,
                    "journal-id": "fixture",
                    "archive": {
                        "enabled": enabled,
                        "adapter": "local",
                        "path": str(tmp_path / "archive"),
                        "sink-id": "fixture",
                        "poll-interval-seconds": 0.01,
                    },
                },
                "data": {
                    "relational": {
                        "enabled": False,
                        "url": f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
                        "ddl-auto": "create",
                    }
                },
            }
        }
    )


async def test_disabled_archive_creates_no_paths_or_implicit_keys(tmp_path):
    archive = feature("archive_auto_configuration", "AuditArchiveAutoConfiguration")
    context = ApplicationContext(configuration(tmp_path, enabled=False), auto_configurations=[archive])
    await context.start()
    try:
        assert not context.container.contains_type(feature("archive", "AuditArchiveDelivery"))
        assert not (tmp_path / "archive").exists()
    finally:
        await context.stop()


async def test_enabled_archive_requires_both_explicit_key_ports_before_lifecycle(tmp_path):
    archive = feature("archive_auto_configuration", "AuditArchiveAutoConfiguration")
    context = ApplicationContext(configuration(tmp_path), auto_configurations=[AuditAutoConfiguration, archive])
    with pytest.raises(NoSuchBeanError, match="CheckpointSigner|CheckpointVerifier"):
        await context.start()
    assert not (tmp_path / "archive").exists()
    await context.stop()


async def test_explicit_native_di_borrows_key_ports_and_stops_worker_before_local_descriptors(tmp_path):
    archive = feature("archive_auto_configuration", "AuditArchiveAutoConfiguration")
    signer_cls = feature("signing", "Ed25519CheckpointSigner")
    verifier_cls = feature("signing", "Ed25519CheckpointVerifier")
    key = Ed25519PrivateKey.generate()
    signer, verifier = signer_cls("fixture-key", lambda ref: key), verifier_cls(lambda ref: key.public_key())
    context = ApplicationContext(configuration(tmp_path), auto_configurations=[AuditAutoConfiguration, archive])
    context.container.register_instance(CheckpointSigner, signer)
    context.container.register_instance(CheckpointVerifier, verifier)
    await context.start()
    try:
        delivery = context.get_bean(feature("archive", "AuditArchiveDelivery"))
        sink = context.get_bean(feature("archive_ports", "ArchiveSink"))
        references = context.get_bean(feature("archive_ports", "ArchiveReceiptStore"))
        assert sink is references
        assert not delivery.capabilities().retention_lock and not delivery.capabilities().independent_custody
        journal = context.get_bean(AuditJournal)
        async with infrastructure_unit(journal.engine):
            entry = await journal.append(event("configured").model_copy(update={"scope": SCOPE}))
        identifier = feature("archive_models", "ArchiveSegment").from_entries((entry,)).manifest.delivery_id
        receipt = None
        for _ in range(100):
            receipt = await references.lookup(identifier)
            if receipt is not None:
                break
            await asyncio.sleep(0.01)
        assert receipt is not None
        assert (await feature("archive", "verify_archive")(sink, receipt, verifier)).entries == (entry,)
    finally:
        await context.stop()
    assert await verifier.verify(b"still borrowed", await signer.sign(b"still borrowed"))
    with pytest.raises(RuntimeError, match="Start"):
        await sink.lookup(identifier)


async def test_host_delivery_override_does_not_create_default_paths_or_require_default_ports(tmp_path):
    archive = feature("archive_auto_configuration", "AuditArchiveAutoConfiguration")
    delivery_type = feature("archive", "AuditArchiveDelivery")
    context = ApplicationContext(configuration(tmp_path), auto_configurations=[archive])
    existing = object()
    context.container.register_instance(delivery_type, existing)
    await context.start()
    try:
        assert context.get_bean(delivery_type) is existing
        assert not (tmp_path / "archive").exists()
        assert not context.container.contains_type(feature("archive_ports", "ArchiveSink"))
    finally:
        await context.stop()
