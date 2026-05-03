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
"""Transaction ID filter — propagates or generates X-Transaction-Id."""

from __future__ import annotations

import uuid
from typing import cast

from starlette.requests import Request
from starlette.responses import Response

from pyfly.container.ordering import HIGHEST_PRECEDENCE, order
from pyfly.web.filters import OncePerRequestFilter
from pyfly.web.ports.filter import CallNext

TRANSACTION_ID_HEADER = "X-Transaction-Id"


@order(HIGHEST_PRECEDENCE + 100)
class TransactionIdFilter(OncePerRequestFilter):
    """Injects or propagates ``X-Transaction-Id`` on every request/response."""

    async def do_filter(self, request: Request, call_next: CallNext) -> Response:
        tx_id = request.headers.get(TRANSACTION_ID_HEADER) or str(uuid.uuid4())
        request.state.transaction_id = tx_id
        response = cast(Response, await call_next(request))
        response.headers[TRANSACTION_ID_HEADER] = tx_id
        return response
