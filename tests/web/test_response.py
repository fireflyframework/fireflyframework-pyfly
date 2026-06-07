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
"""Tests for return value handler."""

import json

from pydantic import BaseModel
from starlette.responses import HTMLResponse

from pyfly.web.adapters.starlette.response import handle_return_value


class ItemResponse(BaseModel):
    id: str
    name: str


class TestHandleReturnValue:
    def test_pydantic_model(self):
        item = ItemResponse(id="1", name="Widget")
        response = handle_return_value(item)
        assert response.media_type == "application/json"
        assert response.status_code == 200
        assert json.loads(response.body) == {"id": "1", "name": "Widget"}

    def test_pydantic_model_custom_status(self):
        item = ItemResponse(id="1", name="Widget")
        response = handle_return_value(item, status_code=201)
        assert response.status_code == 201

    def test_dict(self):
        response = handle_return_value({"key": "value"})
        assert response.media_type == "application/json"
        assert response.status_code == 200
        assert json.loads(response.body) == {"key": "value"}

    def test_none_returns_no_content(self):
        response = handle_return_value(None)
        assert response.status_code == 204

    def test_none_with_explicit_status(self):
        response = handle_return_value(None, status_code=202)
        assert response.status_code == 202

    def test_starlette_response_passthrough(self):
        html = HTMLResponse("<h1>Hello</h1>")
        response = handle_return_value(html)
        assert response is html

    def test_list(self):
        response = handle_return_value([1, 2, 3])
        assert response.media_type == "application/json"
        assert json.loads(response.body) == [1, 2, 3]

    def test_string(self):
        response = handle_return_value("hello")
        assert response.media_type == "application/json"
        assert json.loads(response.body) == "hello"
