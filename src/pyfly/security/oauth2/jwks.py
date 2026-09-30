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
"""Public borrowed transport contract for fixed-endpoint JWKS retrieval."""

from typing import Protocol


class JWKSFetcher(Protocol):
    """Synchronous borrowed callable; the validator never closes it.

    Return raw JSON bytes from the configured URI. The implementation is trusted
    to enforce egress policy, HTTP success, no redirects, identity encoding and
    timeout/byte limits while reading. PyFly independently checks the returned
    type, length, JSON and signing keys. No token-supplied URI is passed here.

    ``timeout`` is the remaining cooperative refresh budget, including the
    caller's remaining validation budget when supplied. Synchronous work cannot
    be forcibly cancelled: return or raise promptly, and own transport cleanup.
    PyFly rejects late results before publishing a key snapshot.
    """

    def __call__(self, uri: str, *, timeout: float, max_bytes: int) -> bytes: ...


class AsyncJWKSFetcher(Protocol):
    """Asynchronous borrowed callable with the same HTTP duties as JWKSFetcher.

    Await network I/O directly, honor cancellation and complete transport cleanup
    before returning or raising. The validator awaits this callable in its caller's
    task; it never closes the borrowed transport or starts a background worker.
    Cancellation and deadlines are cooperative, including transport cleanup.
    """

    async def __call__(self, uri: str, *, timeout: float, max_bytes: int) -> bytes: ...
