# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Durable conditional-create local archive; unlocked, owner-mutable and never WORM."""

from __future__ import annotations

import asyncio
import contextlib
import os
import stat
import threading
import uuid
from collections.abc import Callable
from pathlib import Path

from pydantic import ConfigDict, TypeAdapter

from pyfly.audit.archive_models import (
    MAX_ARCHIVE_BYTES,
    ArchiveCapabilities,
    ArchiveDeliveryReceipt,
    ArchiveReceipt,
    byte_limit,
    content_digest,
)
from pyfly.audit.models import Digest, Identifier, canonical_bytes
from pyfly.audit.ports import AuditConflict, AuditError, AuditIntegrityError


class LocalArchiveAdapter:
    """Owns only its directory descriptors; callers supply a trusted private parent directory."""

    def __init__(self, root: str | Path, *, sink_id: str, max_bytes: int = MAX_ARCHIVE_BYTES) -> None:
        byte_limit(max_bytes)
        self._root = Path(root).absolute()
        self._sink_id = TypeAdapter(Identifier, config=ConfigDict(hide_input_in_errors=True)).validate_python(sink_id)
        self._max_bytes = max_bytes
        self._guard = threading.RLock()
        self._fds: dict[str, int] = {}

    def capabilities(self) -> ArchiveCapabilities:
        return ArchiveCapabilities(
            sink_id=self._sink_id,
            backend="local-filesystem",
            conditional_create=True,
            durable_ack=True,
            versioned=False,
            retention_lock=False,
            independent_custody=False,
        )

    async def start(self) -> None:
        await self._lifecycle(self._start, close_on_cancel=True)

    @staticmethod
    async def _drain(worker: asyncio.Task[None]) -> None:
        while not worker.done():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.shield(worker)
        if not worker.cancelled():
            worker.exception()

    async def _lifecycle(self, operation: Callable[[], None], *, close_on_cancel: bool) -> None:
        worker = asyncio.create_task(asyncio.to_thread(operation))
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            await self._drain(worker)
            if close_on_cancel:
                await self._drain(asyncio.create_task(asyncio.to_thread(self._stop)))
            raise asyncio.CancelledError from None

    def _start(self) -> None:
        with self._guard:
            if self._fds:
                return
            opened: dict[str, int] = {}
            try:
                with contextlib.suppress(FileExistsError):
                    self._root.mkdir(mode=0o700)
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                opened["root"] = os.open(self._root, flags)
                for name in ("objects", "refs"):
                    with contextlib.suppress(FileExistsError):
                        os.mkdir(name, mode=0o700, dir_fd=opened["root"])
                    opened[name] = os.open(name, flags, dir_fd=opened["root"])
                for descriptor in opened.values():
                    info = os.fstat(descriptor)
                    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                        raise AuditError("Local archive directories must be private and owned by the caller")
                os.fsync(opened["root"])
                parent = os.open(self._root.parent, flags)
                try:
                    os.fsync(parent)
                finally:
                    os.close(parent)
            except BaseException:
                for descriptor in opened.values():
                    with contextlib.suppress(OSError):
                        os.close(descriptor)
                raise AuditError("Local archive directory initialization failed") from None
            self._fds = opened

    async def stop(self) -> None:
        await self._lifecycle(self._stop, close_on_cancel=False)

    def _stop(self) -> None:
        with self._guard:
            descriptors, self._fds = self._fds, {}
            for descriptor in descriptors.values():
                os.close(descriptor)

    def _directory(self, name: str) -> int:
        if not self._fds:
            raise AuditError("Start the local archive before use")
        return self._fds[name]

    @staticmethod
    def _read(directory: int, name: str, maximum: int) -> bytes:
        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
                    raise AuditIntegrityError("Archive object is not a bounded regular file")
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = os.read(descriptor, min(65536, maximum + 1 - total))
                    if not chunk:
                        return b"".join(chunks)
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > maximum:
                        raise AuditIntegrityError("Archive object exceeds its byte bound")
            finally:
                os.close(descriptor)
        except FileNotFoundError:
            raise
        except OSError:
            raise AuditIntegrityError("Archive object cannot be read safely") from None

    @classmethod
    def _publish(cls, directory: int, name: str, content: bytes, *, projection: bool = False) -> None:
        temporary = ".pending-" + uuid.uuid4().hex
        descriptor = None
        accepted = False
        try:
            descriptor = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
            )
            offset = 0
            while offset < len(content):
                written = os.write(descriptor, content[offset:])
                if written <= 0:
                    raise OSError("Incomplete archive write")
                offset += written
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            try:
                os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
            except FileExistsError:
                if cls._read(directory, name, 32768 if projection else len(content)) != content:
                    if projection:
                        raise AuditConflict("Archive delivery reference already identifies different facts") from None
                    raise AuditIntegrityError("Existing archive content differs from its immutable identity") from None
                existing = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                try:
                    os.fsync(existing)
                finally:
                    os.close(existing)
            os.fsync(directory)
            accepted = True
        except OSError:
            raise AuditError("Local archive durable publication failed") from None
        finally:
            try:
                if descriptor is not None:
                    os.close(descriptor)
                if accepted:
                    os.unlink(temporary, dir_fd=directory)
                    os.fsync(directory)
            except OSError:
                raise AuditError("Local archive durable publication cleanup failed") from None
            # Failed attempts remain private and unaccepted for explicit operator inspection.

    async def put(self, content_id: str, content: bytes) -> ArchiveReceipt:
        content_id = TypeAdapter(Digest, config=ConfigDict(hide_input_in_errors=True)).validate_python(content_id)
        if type(content) is not bytes or len(content) > self._max_bytes:
            raise ValueError("Archive content must be bounded immutable bytes")
        if content_digest(content) != content_id:
            raise AuditIntegrityError("Archive content does not match its content identity")
        await asyncio.to_thread(self._put, content_id, content)
        return ArchiveReceipt(
            sink_id=self._sink_id,
            content_id=content_id,
            byte_count=len(content),
            object_ref=content_id,
            retention_status="unlocked",
        )

    def _put(self, content_id: str, content: bytes) -> None:
        with self._guard:
            self._publish(self._directory("objects"), content_id, content)

    async def get(self, receipt: ArchiveReceipt) -> bytes:
        receipt = ArchiveReceipt.model_validate(receipt)
        if (
            receipt.sink_id != self._sink_id
            or receipt.object_ref != receipt.content_id
            or receipt.version_ref is not None
            or receipt.retention_status != "unlocked"
        ):
            raise AuditIntegrityError("Archive receipt does not identify this local adapter")
        return await asyncio.to_thread(self._get, receipt)

    def _get(self, receipt: ArchiveReceipt) -> bytes:
        with self._guard:
            try:
                content = self._read(self._directory("objects"), receipt.content_id, self._max_bytes)
            except FileNotFoundError:
                raise AuditIntegrityError("Archive object is missing") from None
            if len(content) != receipt.byte_count or content_digest(content) != receipt.content_id:
                raise AuditIntegrityError("Archive object failed content verification")
            return content

    async def record(self, delivery_id: str, receipt: ArchiveDeliveryReceipt) -> None:
        delivery_id = TypeAdapter(Digest, config=ConfigDict(hide_input_in_errors=True)).validate_python(delivery_id)
        receipt = ArchiveDeliveryReceipt.model_validate(receipt)
        if receipt.delivery_id != delivery_id:
            raise AuditConflict("Archive reference key does not match its delivery")
        content = canonical_bytes(receipt.model_dump(mode="json"))
        if len(content) > 32768:
            raise ValueError("Archive delivery reference exceeds its byte bound")
        await asyncio.to_thread(self._record, delivery_id, content)

    def _record(self, delivery_id: str, content: bytes) -> None:
        with self._guard:
            self._publish(self._directory("refs"), delivery_id, content, projection=True)

    async def lookup(self, delivery_id: str) -> ArchiveDeliveryReceipt | None:
        delivery_id = TypeAdapter(Digest, config=ConfigDict(hide_input_in_errors=True)).validate_python(delivery_id)
        return await asyncio.to_thread(self._lookup, delivery_id)

    def _lookup(self, delivery_id: str) -> ArchiveDeliveryReceipt | None:
        with self._guard:
            try:
                content = self._read(self._directory("refs"), delivery_id, 32768)
            except FileNotFoundError:
                return None
            try:
                receipt = ArchiveDeliveryReceipt.model_validate_json(content)
                if receipt.delivery_id != delivery_id or canonical_bytes(receipt.model_dump(mode="json")) != content:
                    raise ValueError
                return receipt
            except ValueError:
                raise AuditIntegrityError("Archive delivery reference failed verification") from None
