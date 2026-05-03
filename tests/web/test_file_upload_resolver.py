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
"""Tests for File[T] resolution in ParameterResolver with Starlette."""

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient


async def upload_single(request: Request) -> JSONResponse:
    """Handler that receives a single file upload."""
    form = await request.form()
    upload = form.get("file")
    if upload is None:
        return JSONResponse({"error": "no file"}, status_code=400)
    content = await upload.read()
    return JSONResponse(
        {
            "filename": upload.filename,
            "content_type": upload.content_type,
            "size": len(content),
        }
    )


@pytest.fixture
def client():
    app = Starlette(routes=[Route("/upload", upload_single, methods=["POST"])])
    return TestClient(app)


class TestFileUploadResolver:
    def test_single_file_upload(self, client):
        response = client.post(
            "/upload",
            files={"file": ("test.txt", b"hello world", "text/plain")},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["filename"] == "test.txt"
        assert data["content_type"] == "text/plain"
        assert data["size"] == 11

    def test_no_file(self, client):
        response = client.post("/upload")
        assert response.status_code == 400
