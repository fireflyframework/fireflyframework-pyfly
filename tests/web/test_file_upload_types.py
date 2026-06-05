"""Tests for File[T] and UploadedFile types."""

import io
from typing import get_args, get_origin

import pytest

from pyfly.web.params import File, UploadedFile, inspect_binding


class TestUploadedFile:
    @pytest.mark.asyncio
    async def test_read(self):
        content = b"hello world"
        uf = UploadedFile(
            filename="test.txt",
            content_type="text/plain",
            size=len(content),
            _file=io.BytesIO(content),
        )
        assert await uf.read() == content

    @pytest.mark.asyncio
    async def test_save(self, tmp_path):
        content = b"file content"
        uf = UploadedFile(
            filename="test.txt",
            content_type="text/plain",
            size=len(content),
            _file=io.BytesIO(content),
        )
        target = tmp_path / "saved.txt"
        await uf.save(target)
        assert target.read_bytes() == content

    def test_properties(self):
        uf = UploadedFile(
            filename="photo.jpg",
            content_type="image/jpeg",
            size=1024,
            _file=io.BytesIO(b"x" * 1024),
        )
        assert uf.filename == "photo.jpg"
        assert uf.content_type == "image/jpeg"
        assert uf.size == 1024


class TestFileGeneric:
    def test_file_is_generic(self):
        """File[UploadedFile] should be introspectable."""
        hint = File[UploadedFile]
        assert inspect_binding(hint)[0] is File
        args = get_args(hint)
        assert args[0] is UploadedFile

    def test_file_list(self):
        """File[list[UploadedFile]] for multi-file upload."""
        hint = File[list[UploadedFile]]
        assert inspect_binding(hint)[0] is File
        inner = get_args(hint)[0]
        assert get_origin(inner) is list
