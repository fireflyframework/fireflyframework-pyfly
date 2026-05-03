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
"""Exception handlers — reuses the Starlette global exception handler."""

from __future__ import annotations

from typing import Any


def register_exception_handlers(app: Any) -> None:
    """Register the global exception handler on a FastAPI application.

    FastAPI extends Starlette, so the same handler works unchanged.
    """
    from pyfly.web.adapters.starlette.errors import global_exception_handler

    app.add_exception_handler(Exception, global_exception_handler)
