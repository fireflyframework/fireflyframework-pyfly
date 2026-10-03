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
"""``FlagManagement``: the operations of the ``flags`` management API (spec 4.8), shared by the actuator endpoint,
the admin page and ``pyfly flags``.

Reads list the provider, the sources and the effective flags, or one flag with every layer's definition and its
store history. ``evaluate`` previews an evaluation with the explicit context and the process attributes only (not
the caller's principal) and is never counted as an exposure. Writes need ``pyfly.feature-flags.management.writes``
and a store; ``enable``/``disable``/``default-variant`` on a flag the store does not hold copy the current effective
definition into the store first. Every failure is a :class:`FlagManagementError` with a contract code.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pyfly.feature_flags.client import typed_default
from pyfly.feature_flags.definitions import FlagDefinitionError, flag_type, is_expired, utc_today
from pyfly.feature_flags.store.ports import FlagConflictError, FlagNotStoredError

if TYPE_CHECKING:
    from openfeature.flag_evaluation import FlagValueType

    from pyfly.feature_flags.client import FeatureFlags
    from pyfly.feature_flags.composition import ComposedFlag
    from pyfly.feature_flags.registry import FlagRegistry, SourceStatus
    from pyfly.feature_flags.store.ports import FlagStore
    from pyfly.feature_flags.store.writer import FlagStoreWriter

__all__ = [
    "ERROR_STATUS",
    "HISTORY_LIMIT",
    "WRITE_ACTIONS",
    "FlagManagement",
    "FlagManagementError",
    "actor_from_security",
    "iso_instant",
]

_logger = logging.getLogger(__name__)


def _warn(message: str, **kwargs: Any) -> None:
    with suppress(Exception):
        _logger.warning(message, **kwargs)


ERROR_STATUS: dict[str, int] = {
    "writes-disabled": 403,
    "not-writable": 409,
    "invalid-definition": 422,
    "unknown-flag": 404,
    "unknown-variant": 422,
    "conflict": 409,
    "bad-request": 400,
}
WRITE_ACTIONS: tuple[str, ...] = ("enable", "disable", "default-variant", "put", "delete")
HISTORY_LIMIT = 50


class FlagManagementError(Exception):
    """A refused management operation: ``code`` is the portable contract code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message

    @property
    def status(self) -> int:
        """The HTTP status of the code (the admin API answers it; the actuator always answers 400)."""
        return ERROR_STATUS[self.code]

    def to_body(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message}


def iso_instant(value: datetime | None) -> str | None:
    """*value* as UTC ISO-8601 with seconds and a ``Z`` (``2026-10-01T09:30:00Z``)."""
    if value is None:
        return None
    instant = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return instant.isoformat(timespec="seconds").replace("+00:00", "Z")


def actor_from_security(fallback: str) -> str:
    """The authenticated principal's ``user_id``, or *fallback* (``actuator``, ``admin``, ``cli:<user>``)."""
    from pyfly.security.context_holder import SecurityContextHolder

    context = SecurityContextHolder.get_context()
    if context is not None and context.is_authenticated and context.user_id:
        return context.user_id
    return fallback


def _source(status: SourceStatus) -> dict[str, Any]:
    return {
        "name": status.name,
        "enabled": status.enabled,
        "status": status.status,
        "flags": status.flags,
        "lastRefresh": iso_instant(status.last_refresh),
        "error": status.error,
        "revision": status.revision,
    }


def _summary(flag: ComposedFlag, version: int | None) -> dict[str, Any]:
    definition = flag.definition
    return {
        "key": flag.key,
        "state": definition["state"],
        "type": flag_type(definition),
        "variants": list(definition["variants"]),
        "defaultVariant": definition.get("defaultVariant"),
        "targeting": bool(definition.get("targeting")),
        "origin": flag.origin,
        "overrides": list(flag.overrides),
        "metadata": dict(definition.get("metadata") or {}),
        "expired": is_expired(definition, utc_today()),
        "version": version,
    }


def _expected_version(raw: Any) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise FlagManagementError("bad-request", "expectedVersion must be a non-negative integer")
    return raw


class FlagManagement:
    """The management operations (see the module documentation)."""

    def __init__(
        self,
        facade: FeatureFlags,
        *,
        registry: FlagRegistry | None = None,
        store: FlagStore | None = None,
        writer: FlagStoreWriter | None = None,
        writes_enabled: bool = False,
    ) -> None:
        self._facade = facade
        self._registry = registry
        self._store = store
        self._writer = writer
        self._writes_enabled = writes_enabled

    @property
    def facade(self) -> FeatureFlags:
        return self._facade

    @property
    def registry(self) -> FlagRegistry | None:
        return self._registry

    @property
    def writable(self) -> bool:
        return self._store is not None and self._writer is not None

    @property
    def writes_enabled(self) -> bool:
        return self._writes_enabled

    # -- reads -----------------------------------------------------------------------------------------------

    async def _versions(self) -> dict[str, int]:
        if self._store is None:
            return {}
        try:
            return {key: row.version for key, row in (await self._store.all()).items()}
        except Exception:  # noqa: BLE001 — the overview still answers, without versions
            _warn("feature_flag_store_unavailable", exc_info=True)
            return {}

    async def overview(self) -> dict[str, Any]:
        client = self._facade.client
        provider = {"name": client.provider.get_metadata().name, "status": client.get_provider_status().value}
        registry = self._registry
        flags: list[dict[str, Any]] = []
        if registry is not None:
            versions = await self._versions()
            composition = registry.composition()
            flags = [_summary(composition.flags[key], versions.get(key)) for key in sorted(composition.flags)]
        return {
            "provider": provider,
            "writable": self.writable,
            "writesEnabled": self._writes_enabled,
            "sources": [_source(status) for status in registry.sources()] if registry is not None else [],
            "flags": flags,
        }

    async def detail(self, key: str) -> dict[str, Any] | None:
        flag = self._registry.effective_flag(key) if self._registry is not None else None
        if flag is None or self._registry is None:
            return None
        version: int | None = None
        history: list[dict[str, Any]] = []
        if self._store is not None:
            try:
                stored = await self._store.get(key)
                version = stored.version if stored is not None else None
                history = [
                    {
                        "id": change.id,
                        "action": change.action,
                        "actor": change.actor,
                        "changedAt": iso_instant(change.changed_at),
                    }
                    for change in await self._store.history(key, HISTORY_LIMIT)
                ]
            except Exception:  # noqa: BLE001 — the detail still answers, without the store's part
                _warn("feature_flag_store_unavailable", extra={"flag": key}, exc_info=True)
        return {
            "key": key,
            "definition": flag.definition,
            "origin": flag.origin,
            "layers": [
                {"source": source, "definition": definition} for source, definition in self._registry.layers(key)
            ],
            "version": version,
            "expired": is_expired(flag.definition, utc_today()),
            "history": history,
        }

    # -- the POST operations ---------------------------------------------------------------------------------

    async def execute(self, key: str, body: Mapping[str, Any], *, actor: str) -> dict[str, Any]:
        action = body.get("action")
        if not isinstance(action, str) or not action:
            raise FlagManagementError("bad-request", "the body needs an action")
        if action == "evaluate":
            return await self.evaluate(key, context=body.get("context"), targeting_key=body.get("targetingKey"))
        if action not in WRITE_ACTIONS:
            raise FlagManagementError("bad-request", f"unknown action {action!r}: evaluate, {', '.join(WRITE_ACTIONS)}")
        self._require_writes()
        expected = _expected_version(body.get("expectedVersion"))
        if action == "put":
            return await self.put(key, body.get("definition"), actor=actor, expected_version=expected)
        if action == "delete":
            return await self.delete(key, actor=actor, expected_version=expected)
        if action in ("enable", "disable"):
            return await self.set_state(key, enabled=action == "enable", actor=actor, expected_version=expected)
        variant = body.get("variant")
        if not isinstance(variant, str) or not variant:
            raise FlagManagementError("bad-request", "default-variant needs a variant")
        return await self.set_default_variant(key, variant, actor=actor, expected_version=expected)

    async def evaluate(self, key: str, *, context: Any = None, targeting_key: Any = None) -> dict[str, Any]:
        if context is not None and not isinstance(context, Mapping):
            raise FlagManagementError("bad-request", "context must be an object")
        if targeting_key is not None and not isinstance(targeting_key, str):
            raise FlagManagementError("bad-request", "targetingKey must be a string")
        default: FlagValueType = False
        if self._registry is not None:
            flag = self._registry.effective_flag(key)
            if flag is None:
                raise FlagManagementError("unknown-flag", f"no layer defines {key!r}")
            default = typed_default(flag.definition)
        details = await self._facade.details_async(
            key, default, context=dict(context or {}), targeting_key=targeting_key, ambient=False, preview=True
        )
        return {
            "key": key,
            "value": details.value,
            "variant": details.variant,
            "reason": str(details.reason) if details.reason is not None else None,
            "errorCode": details.error_code.value if details.error_code is not None else None,
            "metadata": dict(details.flag_metadata),
        }

    def _require_writes(self) -> None:
        if not self._writes_enabled:
            raise FlagManagementError(
                "writes-disabled", "runtime flag writes are disabled (pyfly.feature-flags.management.writes)"
            )
        if not self.writable:
            raise FlagManagementError(
                "not-writable", "no flag store is configured (pyfly.feature-flags.sources.store.enabled)"
            )

    async def put(
        self, key: str, definition: Any, *, actor: str, expected_version: int | None = None
    ) -> dict[str, Any]:
        self._require_writes()
        expected_version = _expected_version(expected_version)
        assert self._writer is not None
        if not isinstance(definition, Mapping):
            raise FlagManagementError(
                "invalid-definition", f"invalid feature flag {key!r}: flag definition must be an object"
            )
        try:
            await self._writer.put(key, definition, actor=actor, expected_version=expected_version)
        except FlagDefinitionError as error:
            raise FlagManagementError("invalid-definition", str(error)) from error
        except FlagConflictError as error:
            raise FlagManagementError("conflict", str(error)) from error
        return await self._after_write(key, action="put")

    async def delete(self, key: str, *, actor: str, expected_version: int | None = None) -> dict[str, Any]:
        self._require_writes()
        expected_version = _expected_version(expected_version)
        assert self._writer is not None
        try:
            await self._writer.delete(key, actor=actor, expected_version=expected_version)
        except FlagNotStoredError as error:
            raise FlagManagementError("unknown-flag", str(error)) from error
        except FlagConflictError as error:
            raise FlagManagementError("conflict", str(error)) from error
        return await self._after_write(key, action="delete")

    async def set_state(
        self, key: str, *, enabled: bool, actor: str, expected_version: int | None = None
    ) -> dict[str, Any]:
        current = await self._current(key)
        changed = {**current, "state": "ENABLED" if enabled else "DISABLED"}
        return await self.put(key, changed, actor=actor, expected_version=expected_version)

    async def set_default_variant(
        self, key: str, variant: str, *, actor: str, expected_version: int | None = None
    ) -> dict[str, Any]:
        current = await self._current(key)
        if variant not in (current.get("variants") or {}):
            raise FlagManagementError("unknown-variant", f"{variant!r} is not a variant of {key!r}")
        return await self.put(
            key, {**current, "defaultVariant": variant}, actor=actor, expected_version=expected_version
        )

    async def _current(self, key: str) -> dict[str, Any]:
        """The store's definition of *key*, else its effective definition (copied into the store by the write)."""
        self._require_writes()
        assert self._store is not None
        stored = await self._store.get(key)
        if stored is not None:
            return dict(stored.definition)
        flag = self._registry.effective_flag(key) if self._registry is not None else None
        if flag is None:
            raise FlagManagementError("unknown-flag", f"no layer defines {key!r}")
        return dict(flag.definition)

    async def _after_write(self, key: str, *, action: str) -> dict[str, Any]:
        try:
            detail = await self.detail(key)
        except Exception:  # noqa: BLE001 — diagnostics cannot reject an accepted write
            _warn("feature_flag_management_detail_failed", extra={"flag": key}, exc_info=True)
            detail = None
        if detail is not None:
            return detail
        return {"key": key, "refreshPending" if action == "put" else "deleted": True}
