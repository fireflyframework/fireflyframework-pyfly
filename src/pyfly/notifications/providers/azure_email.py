# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Optional official async ACS adapter; acceptance is distinct from delivery.

ACS Email retires September 30, 2028. Keep provider selection replaceable and
review Microsoft's current lifecycle guidance before deploying a new workload.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import logging
import math
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5

from pyfly.notifications._email import message_headers, secure_endpoint
from pyfly.notifications.capabilities import EmailProviderCapabilities
from pyfly.notifications.models import EmailMessage, EmailStatus, NotificationResult

if TYPE_CHECKING:
    from azure.core.credentials_async import AsyncTokenCredential

logger = logging.getLogger(__name__)


class AzureCommunicationEmailProvider:
    name = "azure-communication-email"
    capabilities = EmailProviderCapabilities(real_delivery=True, inline_cid=True, operation_lookup=True)

    def __init__(
        self,
        endpoint: str,
        *,
        credential: AsyncTokenCredential | None = None,
        client: Any | None = None,
        managed_identity: bool = False,
        managed_identity_client_id: str | None = None,
        default_from: str | None = None,
        timeout: float = 10,
        poll_timeout: float = 30,
        poll_interval: float = 1,
    ) -> None:
        self._endpoint = secure_endpoint(endpoint)
        if urlsplit(self._endpoint).path:
            raise ValueError("Azure Email requires an HTTPS resource origin without a path")
        if type(managed_identity) is not bool or (client is None and credential is None and not managed_identity):
            raise ValueError("Azure Email requires an explicit managed identity or injected credential/client")
        if client is not None and (credential is not None or managed_identity):
            raise ValueError("Configure an injected Azure Email client or credentials, not both")
        if credential is not None and managed_identity:
            raise ValueError("Configure an injected credential or managed identity, not both")
        if credential is not None and not inspect.iscoroutinefunction(getattr(credential, "get_token", None)):
            raise ValueError("Azure Email requires an asynchronous token credential")
        if managed_identity_client_id is not None and (
            not isinstance(managed_identity_client_id, str)
            or not managed_identity_client_id
            or any(c in managed_identity_client_id for c in "\r\n\0")
        ):
            raise ValueError("Managed identity client ID must be a non-empty string")
        if managed_identity_client_id is not None and not managed_identity:
            raise ValueError("A managed identity client ID requires managed identity authentication")
        for value, maximum in ((timeout, 300), (poll_timeout, 900), (poll_interval, 30)):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not 0 < value <= maximum
            ):
                raise ValueError("Azure Email timeouts and polling interval must be finite and bounded")
        self._client, self._credential = client, credential
        self._owns_client, self._owns_credential = client is None, False
        self._managed_identity_client_id = managed_identity_client_id
        self._default_from = default_from
        self._timeout, self._poll_timeout, self._poll_interval = timeout, poll_timeout, poll_interval
        self._lock, self._closed = asyncio.Lock(), False

    @staticmethod
    def operation_id(message_id: str) -> str:
        if not isinstance(message_id, str) or not message_id:
            raise ValueError("A stable email message ID is required")
        try:
            return str(UUID(message_id))
        except ValueError:
            return str(uuid5(NAMESPACE_URL, "pyfly-email:" + message_id))

    async def _ensure_client(self) -> Any:
        async with self._lock:
            if self._closed:
                raise RuntimeError("EMAIL_PROVIDER_CLOSED")
            if self._client is not None:
                return self._client
            try:
                from azure.communication.email.aio import EmailClient
                from azure.core.pipeline.policies import (
                    AsyncBearerTokenCredentialPolicy,
                    AsyncRedirectPolicy,
                    AsyncRetryPolicy,
                    ContentDecodePolicy,
                    CustomHookPolicy,
                    HeadersPolicy,
                    NetworkTraceLoggingPolicy,
                    RequestIdPolicy,
                    UserAgentPolicy,
                )
            except ImportError:
                raise ImportError("Azure Email requires pyfly[notifications-azure]") from None
            if self._credential is None:
                from azure.identity.aio import ManagedIdentityCredential

                self._credential = ManagedIdentityCredential(client_id=self._managed_identity_client_id)
                self._owns_credential = True

            class NoReplayBearerPolicy(AsyncBearerTokenCredentialPolicy[Any, Any]):
                async def on_challenge(self, request: Any, response: Any) -> bool:
                    # A credential challenge is not permission to replay an email POST.
                    return False

            try:
                policies = [
                    RequestIdPolicy(),
                    HeadersPolicy(),
                    UserAgentPolicy(),
                    ContentDecodePolicy(),
                    AsyncRedirectPolicy(permit_redirects=False),
                    AsyncRetryPolicy(retry_total=0),
                    NoReplayBearerPolicy(self._credential, "https://communication.azure.com//.default"),
                    NetworkTraceLoggingPolicy(logging_enable=False),
                    CustomHookPolicy(),
                ]
                self._client = EmailClient(
                    self._endpoint,
                    self._credential,
                    policies=policies,
                    retry_total=0,
                    permit_redirects=False,
                    logging_enable=False,
                    connection_timeout=self._timeout,
                    read_timeout=self._timeout,
                )
            except Exception:
                if self._owns_credential and self._credential is not None:
                    credential = self._credential
                    self._credential, self._owns_credential = None, False
                    try:
                        async with asyncio.timeout(min(self._timeout, 10)):
                            await credential.close()
                    except Exception:
                        logger.warning(
                            "Email credential cleanup failed", extra={"error_code": "EMAIL_CREDENTIAL_CLEANUP_FAILED"}
                        )
                raise RuntimeError("EMAIL_PROVIDER_CONFIGURATION_FAILED") from None
            return self._client

    async def start(self) -> None:
        # Client/credential construction does not acquire a token or send a request.
        await self._ensure_client()

    async def stop(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            resources = []
            if self._owns_client and self._client is not None:
                resources.append(self._client)
            if self._owns_credential and self._credential is not None:
                resources.append(self._credential)
            self._client, self._credential = None, None
        failed, cancelled = False, False
        for resource in resources:
            try:
                async with asyncio.timeout(min(self._timeout, 10)):
                    await resource.close()
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                failed = True
        if cancelled:
            raise asyncio.CancelledError
        if failed:
            raise RuntimeError("EMAIL_PROVIDER_CLEANUP_FAILED") from None

    def _payload(self, message: EmailMessage) -> dict[str, Any]:
        headers = message_headers(message)
        sender = message.sender or self._default_from
        if not sender:
            raise ValueError("A verified Azure Email sender must be configured")
        content = {"subject": message.subject}
        if message.body_text is not None:
            content["plainText"] = message.body_text
        if message.body_html is not None:
            content["html"] = message.body_html
        recipients = {
            name: [{"address": address} for address in getattr(message, name)]
            for name in ("to", "cc", "bcc")
            if getattr(message, name)
        }
        return {
            "senderAddress": sender,
            "content": content,
            "recipients": recipients,
            "headers": headers,
            "userEngagementTrackingDisabled": True,
            "attachments": [
                {
                    "name": item.filename,
                    "contentType": item.content_type,
                    "contentInBase64": base64.b64encode(item.data).decode("ascii"),
                    **({"contentId": item.content_id} if item.inline else {}),
                }
                for item in message.attachments
            ],
        }

    def _result(
        self, message_id: str, operation: str | None, status: EmailStatus, error: str | None = None
    ) -> NotificationResult:
        return NotificationResult(message_id, self.name, status, provider_id=operation, error=error)

    async def send(self, message: EmailMessage) -> NotificationResult:
        try:
            operation, payload = self.operation_id(message.id), self._payload(message)
        except Exception:
            return self._result(message.id, None, EmailStatus.FAILED, "EMAIL_PREPARATION_FAILED")
        dispatched = False
        try:
            async with asyncio.timeout(self._poll_timeout):
                client = await self._ensure_client()
                # An injected complete client owns its policies; conservatively assume dispatch.
                dispatched = not self._owns_client

                def started(request: Any) -> None:
                    nonlocal dispatched
                    dispatched = True

                poller = await client.begin_send(
                    payload,
                    operation_id=operation,
                    polling=False,
                    retry_total=0,
                    permit_redirects=False,
                    raw_request_hook=started,
                    logging_enable=False,
                    connection_timeout=self._timeout,
                    read_timeout=self._timeout,
                )
                initial = await poller.result()
                if isinstance(initial, dict) and initial.get("id", operation) != operation:
                    return self._result(message.id, operation, EmailStatus.UNKNOWN, "EMAIL_INVALID_PROVIDER_RECEIPT")
                while True:
                    result = await self._lookup(message.id, operation, client)
                    if result.status != EmailStatus.QUEUED:
                        return result
                    await asyncio.sleep(self._poll_interval)
        except TimeoutError:
            return self._result(
                message.id,
                operation,
                EmailStatus.UNKNOWN if dispatched else EmailStatus.FAILED,
                "EMAIL_SEND_OUTCOME_UNKNOWN" if dispatched else "EMAIL_AUTHENTICATION_UNAVAILABLE",
            )
        except Exception as error:
            if not dispatched:
                return self._result(message.id, operation, EmailStatus.FAILED, "EMAIL_PROVIDER_UNAVAILABLE")
            from azure.core.exceptions import HttpResponseError

            status_code = getattr(error, "status_code", None)
            known_unsent = isinstance(error, HttpResponseError) and status_code in {
                400,
                401,
                403,
                404,
                405,
                413,
                415,
                422,
                429,
            }
            return self._result(
                message.id,
                operation,
                EmailStatus.FAILED if known_unsent else EmailStatus.UNKNOWN,
                "EMAIL_SEND_REJECTED" if known_unsent else "EMAIL_SEND_OUTCOME_UNKNOWN",
            )

    async def get_status(self, message_id: str, operation_id: str | None = None) -> NotificationResult:
        try:
            operation = str(UUID(operation_id)) if operation_id is not None else self.operation_id(message_id)
            if operation_id is not None and operation != self.operation_id(message_id):
                raise ValueError("Operation does not match its message")
        except (ValueError, TypeError, AttributeError):
            return self._result(message_id, None, EmailStatus.FAILED, "EMAIL_INVALID_OPERATION")
        try:
            async with asyncio.timeout(self._timeout):
                return await self._lookup(message_id, operation, await self._ensure_client())
        except Exception:
            return self._result(message_id, operation, EmailStatus.UNKNOWN, "EMAIL_STATUS_UNAVAILABLE")

    async def _lookup(self, message_id: str, operation: str, client: Any) -> NotificationResult:
        from azure.core.rest import HttpRequest

        response = None
        try:
            response = await client.send_request(
                HttpRequest("GET", self._endpoint + "/emails/operations/" + operation + "?api-version=2025-09-01"),
                stream=True,
                retry_total=0,
                permit_redirects=False,
                logging_enable=False,
                connection_timeout=self._timeout,
                read_timeout=self._timeout,
            )
            if response.status_code != 200:
                return self._result(message_id, operation, EmailStatus.UNKNOWN, "EMAIL_STATUS_UNAVAILABLE")
            data = bytearray()
            async for chunk in response.iter_bytes():
                if len(data) + len(chunk) > 65536:
                    return self._result(message_id, operation, EmailStatus.UNKNOWN, "EMAIL_INVALID_PROVIDER_RECEIPT")
                data.extend(chunk)
            body = json.loads(data)
            if not isinstance(body, dict) or body.get("id") != operation:
                return self._result(message_id, operation, EmailStatus.UNKNOWN, "EMAIL_INVALID_PROVIDER_RECEIPT")
            status = body.get("status")
            if status == "Succeeded":
                return self._result(message_id, operation, EmailStatus.SENT)
            if status in {"Failed", "Canceled"}:
                return self._result(message_id, operation, EmailStatus.FAILED, "EMAIL_SEND_REJECTED")
            if status in {"Running", "NotStarted"}:
                return self._result(message_id, operation, EmailStatus.QUEUED)
            return self._result(message_id, operation, EmailStatus.UNKNOWN, "EMAIL_INVALID_PROVIDER_RECEIPT")
        except Exception:
            return self._result(message_id, operation, EmailStatus.UNKNOWN, "EMAIL_STATUS_UNAVAILABLE")
        finally:
            if response is not None:
                try:
                    await response.close()
                except Exception:
                    logger.warning(
                        "Email response cleanup failed", extra={"error_code": "EMAIL_RESPONSE_CLEANUP_FAILED"}
                    )
