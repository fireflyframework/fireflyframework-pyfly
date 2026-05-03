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
"""Client registration with an admin server instance."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class AdminClientRegistration:
    """Registers (and deregisters) this application with a remote admin server.

    Uses *httpx* when available; otherwise falls back to
    :mod:`urllib.request` (blocking, run in the default executor).
    """

    def __init__(self, admin_server_url: str, app_name: str, app_url: str) -> None:
        self._server_url = admin_server_url.rstrip("/")
        self._app_name = app_name
        self._app_url = app_url

    # -- Public API -------------------------------------------------------

    async def register(self) -> bool:
        """POST this instance to the admin server's instance registry.

        Returns ``True`` on success, ``False`` on failure (logged).
        """
        endpoint = f"{self._server_url}/admin/api/instances"
        payload = {"name": self._app_name, "url": self._app_url}
        try:
            status, body = await self._post(endpoint, payload)
            if 200 <= status < 300:
                logger.info(
                    "Registered with admin server at %s as '%s'",
                    self._server_url,
                    self._app_name,
                )
                return True
            logger.warning(
                "Admin server registration failed (HTTP %s): %s",
                status,
                body,
            )
            return False
        except Exception:
            logger.exception("Failed to register with admin server at %s", self._server_url)
            return False

    async def deregister(self) -> bool:
        """DELETE this instance from the admin server's instance registry.

        Returns ``True`` on success, ``False`` on failure (logged).
        """
        endpoint = f"{self._server_url}/admin/api/instances/{self._app_name}"
        try:
            status, body = await self._delete(endpoint)
            if 200 <= status < 300:
                logger.info("Deregistered from admin server at %s", self._server_url)
                return True
            logger.warning(
                "Admin server deregistration failed (HTTP %s): %s",
                status,
                body,
            )
            return False
        except Exception:
            logger.exception("Failed to deregister from admin server at %s", self._server_url)
            return False

    # -- HTTP helpers (httpx preferred, urllib fallback) -------------------

    @staticmethod
    async def _post(url: str, payload: dict[str, str]) -> tuple[int, str]:
        try:
            import httpx

            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(url, json=payload)
                return resp.status_code, resp.text
        except ImportError:
            pass

        # Fallback: urllib (blocking, run in executor via asyncio)
        import asyncio
        import json
        import urllib.request

        def _do_post() -> tuple[int, str]:
            data = json.dumps(payload).encode()
            req = urllib.request.Request(
                url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    return resp.status, resp.read().decode()
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read().decode()

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _do_post)

    @staticmethod
    async def _delete(url: str) -> tuple[int, str]:
        try:
            import httpx

            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.delete(url)
                return resp.status_code, resp.text
        except ImportError:
            pass

        import asyncio
        import urllib.request

        def _do_delete() -> tuple[int, str]:
            req = urllib.request.Request(url, method="DELETE")
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    return resp.status, resp.read().decode()
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read().decode()

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _do_delete)
