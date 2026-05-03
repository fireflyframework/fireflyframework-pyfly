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
"""Return value handler -- converts handler return values to Starlette Responses."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from starlette.responses import JSONResponse, Response

from pyfly.web.converters import dict_to_xml


class XMLResponse(Response):
    """Starlette Response that serializes content as ``application/xml``."""

    media_type = "application/xml"


def _wants_xml(accept: str | None) -> bool:
    """Return True when the Accept header indicates an XML preference."""
    if accept is None:
        return False
    return "application/xml" in accept


def _to_json_data(result: Any) -> Any:
    """Normalize a handler result into a JSON-serializable value."""
    if isinstance(result, BaseModel):
        return result.model_dump(mode="json")
    if isinstance(result, list) and result and isinstance(result[0], BaseModel):
        return [item.model_dump(mode="json") for item in result]
    return result


def handle_return_value(
    result: Any,
    status_code: int = 200,
    accept: str | None = None,
) -> Response:
    """Convert a handler's return value into a Starlette Response.

    - ``None`` -> empty response (204 unless status_code explicitly set)
    - ``Response`` -> passed through unchanged
    - ``BaseModel`` -> JSON (or XML when *accept* contains ``application/xml``)
    - ``dict``, ``list``, ``str``, etc. -> JSON (or XML)
    """
    if result is None:
        actual_status = status_code if status_code != 200 else 204
        return Response(status_code=actual_status)

    if isinstance(result, Response):
        return result

    if _wants_xml(accept):
        xml_body = dict_to_xml(result)
        return XMLResponse(content=xml_body, status_code=status_code)

    return JSONResponse(_to_json_data(result), status_code=status_code)
