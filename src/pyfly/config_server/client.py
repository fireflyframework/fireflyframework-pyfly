# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""ConfigClient — fetch config at startup from a remote ConfigServer."""

from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger(__name__)


class ConfigClient:
    """Minimal HTTP client for a Spring-Cloud-Config-style server."""

    def __init__(
        self,
        *,
        url: str,
        application: str,
        profile: str = "default",
        label: str = "main",
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        self._url = url.rstrip("/")
        self._application = application
        self._profile = profile
        self._label = label
        self._username = username
        self._password = password

    async def fetch(self) -> dict[str, Any]:
        try:
            import httpx  # type: ignore[import-not-found, unused-ignore]
        except ImportError as exc:  # noqa: BLE001
            msg = "ConfigClient requires httpx — `pip install pyfly[client]`"
            raise ImportError(msg) from exc

        path = f"{self._url}/{self._application}/{self._profile}/{self._label}"
        auth: tuple[str, str] | None = (
            (self._username, self._password) if self._username is not None and self._password is not None else None
        )
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(path, auth=auth)
            if resp.status_code != 200:
                _logger.warning(
                    "config server returned %d for %s/%s/%s",
                    resp.status_code,
                    self._application,
                    self._profile,
                    self._label,
                )
                return {}
            data = resp.json()
        # Spring orders propertySources HIGHEST priority first, so apply them
        # in reverse (lowest first) and let higher-priority sources overwrite —
        # the forward order let the lowest-priority source win (audit #86).
        merged: dict[str, Any] = {}
        for source in reversed(data.get("propertySources", [])):
            merged.update(source.get("source") or {})
        return merged
