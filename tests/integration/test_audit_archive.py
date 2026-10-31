# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Committed native SQLite/PostgreSQL outbox to durable signed local archive."""

from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pyfly.audit.archive import AuditArchiveDelivery, verify_archive
from pyfly.audit.archive_models import ArchiveSegment
from pyfly.audit.local_archive import LocalArchiveAdapter
from pyfly.audit.signing import Ed25519CheckpointSigner, Ed25519CheckpointVerifier
from pyfly.audit.sql import EXPORT_GROUP, SqlAuditJournal
from pyfly.data.transaction import infrastructure_unit
from tests.integration.test_audit_journal import SCOPE, event
from tests.support.backend_matrix import RelationalBackend

pytestmark = pytest.mark.backends("sqlite-file", "pg")


async def test_committed_native_outbox_produces_verified_restartable_archive(
    relational_backend: RelationalBackend,
    tmp_path: Path,
) -> None:
    journal = SqlAuditJournal(relational_backend.create_engine())
    local = LocalArchiveAdapter(tmp_path / "archive", sink_id="fixture")
    key = Ed25519PrivateKey.generate()
    signer = Ed25519CheckpointSigner("fixture-key", lambda reference: key)
    verifier = Ed25519CheckpointVerifier(lambda reference: key.public_key())
    delivery = AuditArchiveDelivery(journal, journal.outbox, local, local, signer, verifier)
    await journal.start()
    await local.start()
    try:
        with pytest.raises(RuntimeError, match="rollback"):
            async with infrastructure_unit(journal.engine):
                await journal.append(event("rolled-back"))
                raise RuntimeError("rollback")
        assert await delivery.run_once() == 0
        async with infrastructure_unit(journal.engine):
            accepted = await journal.append(event("accepted"))
        assert await delivery.run_once() == 1
        assert not await journal.outbox.pending(EXPORT_GROUP)
        identifier = ArchiveSegment.from_entries((accepted,)).manifest.delivery_id
        receipt = await local.lookup(identifier)
        assert receipt is not None
        await local.stop()
        reopened = LocalArchiveAdapter(tmp_path / "archive", sink_id="fixture")
        await reopened.start()
        try:
            assert await reopened.lookup(identifier) == receipt
            verified = await verify_archive(reopened, receipt, verifier, trusted_head=accepted.head)
            assert verified.entries == (accepted,) and verified.trusted_head_matched
            assert receipt.segment.retention_status == "unlocked"
            assert len(await journal.read(SCOPE)) == 1
        finally:
            await reopened.stop()
    finally:
        await delivery.stop()
        await local.stop()
        await journal.stop()
