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
"""Real httpx-backed :data:`~pyfly.callbacks.dispatcher.HttpSender` factory.

Usage::

    from pyfly.callbacks.adapters.httpx_sender import make_httpx_sender

    sender = make_httpx_sender(timeout=10.0)
    dispatcher = CallbackDispatcher(configs, executions, http=sender)

Requires the ``[client]`` extra (``pip install pyfly[client]``).  The import
is deferred to call-time so the module itself loads even without httpx.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pyfly.callbacks.dispatcher import HttpSender

if TYPE_CHECKING:
    from pyfly.resilience.circuit_breaker import CircuitBreaker

_logger = logging.getLogger(__name__)


def make_httpx_sender(
    *,
    timeout: float = 10.0,
    breaker: CircuitBreaker | None = None,
) -> HttpSender:
    """Return an :data:`HttpSender` that performs a real HTTP POST via *httpx*.

    Parameters
    ----------
    timeout:
        Per-request timeout in seconds passed directly to
        ``httpx.AsyncClient``.  Default: ``10.0``.
    breaker:
        Optional :class:`~pyfly.resilience.circuit_breaker.CircuitBreaker`
        that guards every outbound call.  When the circuit is **open**,
        :meth:`~pyfly.resilience.circuit_breaker.CircuitBreaker.before_call`
        raises :class:`~pyfly.kernel.exceptions.CircuitBreakerException`;
        the dispatcher's ``_deliver`` loop catches it as a generic
        ``Exception``, records ``last_error``, and retries up to
        ``max_attempts`` — the execution is ultimately marked ``FAILED``
        once all attempts are exhausted, but dispatch itself never crashes.
        Transport exceptions (network errors, timeouts) are reported to the
        breaker via ``on_failure``; successful HTTP responses (any status
        code the endpoint returned) close the trip counter via
        ``on_success`` because the *network* call succeeded.

    Returns
    -------
    HttpSender
        An async callable ``(url, payload, headers) -> int`` suitable for
        passing as ``http=`` to :class:`~pyfly.callbacks.dispatcher.CallbackDispatcher`.
    """

    async def _send(url: str, payload: dict[str, Any], headers: dict[str, str]) -> int:
        try:
            import httpx  # type: ignore[import-not-found, unused-ignore]
        except ImportError as exc:
            msg = "make_httpx_sender requires httpx — install the [client] extra: `pip install pyfly[client]`"
            raise ImportError(msg) from exc

        # Raise CircuitBreakerException (caught by dispatcher's except-block)
        # before spending any network resources when the circuit is open.
        if breaker is not None:
            breaker.before_call()

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, json=payload, headers=headers)
        except Exception:
            # Any transport-level error (connect refused, timeout, etc.) is a
            # genuine failure — inform the breaker so consecutive failures can
            # trip the circuit.
            if breaker is not None:
                breaker.on_failure()
            raise

        # The network call reached the server and we got a status code back —
        # that is a network success regardless of what HTTP status was returned.
        if breaker is not None:
            breaker.on_success()

        _logger.debug("POST %s → %d", url, resp.status_code)
        return resp.status_code

    return _send
