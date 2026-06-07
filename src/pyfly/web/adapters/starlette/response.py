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

from starlette.responses import JSONResponse, Response

from pyfly.web.converters import dict_to_xml
from pyfly.web.json import PyFlyJsonSerializer

# Module-level default (as-is config). create_app stashes a config-driven serializer
# on app.state and threads it in; this default applies when none is provided.
_DEFAULT_SERIALIZER = PyFlyJsonSerializer()


class XMLResponse(Response):
    """Starlette Response that serializes content as ``application/xml``."""

    media_type = "application/xml"


def _wants_xml(accept: str | None) -> bool:
    """Return True when the Accept header indicates an XML preference."""
    if accept is None:
        return False
    return "application/xml" in accept


def _to_json_data(result: Any, serializer: PyFlyJsonSerializer | None = None) -> Any:
    """Normalize a handler result into a JSON-serializable value via the serializer.

    Recursive normalization means a list of dicts, a mixed/heterogeneous list, or a
    dict containing models/datetimes is handled — previously only a single model, or a
    list whose first element was a model, was normalized; everything else hit
    ``json.dumps`` and could raise ``TypeError``.
    """
    return (serializer or _DEFAULT_SERIALIZER).to_response_data(result)


def handle_return_value(
    result: Any,
    status_code: int = 200,
    accept: str | None = None,
    serializer: PyFlyJsonSerializer | None = None,
) -> Response:
    """Convert a handler's return value into a Starlette Response.

    - ``None`` -> empty response (204 unless status_code explicitly set)
    - ``Response`` -> passed through unchanged
    - ``BaseModel`` -> JSON (or XML when *accept* contains ``application/xml``)
    - ``dict``, ``list``, ``str``, etc. -> JSON (or XML)

    *serializer* applies global ``pyfly.web.json.*`` config (camelCase / exclude-none /
    custom type encoders); when omitted an as-is default is used.
    """
    if result is None:
        actual_status = status_code if status_code != 200 else 204
        return Response(status_code=actual_status)

    if isinstance(result, Response):
        return result

    if _wants_xml(accept):
        xml_body = dict_to_xml(result)
        return XMLResponse(content=xml_body, status_code=status_code)

    return JSONResponse(_to_json_data(result, serializer), status_code=status_code)
