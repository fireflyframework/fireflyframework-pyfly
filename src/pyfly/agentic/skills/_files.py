# Copyright 2026 Firefly Software Foundation
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

"""POSIX descriptor-relative reads; no check-then-follow symlink window."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from pyfly.agentic.skills._values import MAX_RESOURCE_BYTES


def read_file(
    root: Path,
    path: str,
    *,
    identity: tuple[int, int] | None = None,
    read: bool = True,
) -> tuple[str, tuple[int, int]]:
    descriptors: list[int] = []
    try:
        if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
            raise ValueError("secure skill directory loading requires POSIX descriptor-relative no-follow opens")
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        directory = os.open(root, flags | os.O_DIRECTORY)
        descriptors.append(directory)
        info = os.fstat(directory)
        actual = (info.st_dev, info.st_ino)
        if identity is not None and actual != identity:
            raise ValueError("skill root changed before resource read")
        components = path.split("/")
        for component in components[:-1]:
            directory = os.open(component, flags | os.O_DIRECTORY, dir_fd=directory)
            descriptors.append(directory)
        file = os.open(components[-1], flags, dir_fd=directory)
        descriptors.append(file)
        info = os.fstat(file)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_RESOURCE_BYTES:
            raise ValueError(f"{path!r} must be a regular file of at most {MAX_RESOURCE_BYTES} bytes")
        if not read:
            return "", actual
        data = bytearray()
        while len(data) <= MAX_RESOURCE_BYTES:
            part = os.read(file, min(65_536, MAX_RESOURCE_BYTES + 1 - len(data)))
            if not part:
                break
            data.extend(part)
        if len(data) > MAX_RESOURCE_BYTES:
            raise ValueError(f"{path!r} exceeds {MAX_RESOURCE_BYTES} bytes")
        return data.decode("utf-8"), actual
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"cannot securely read skill file {path!r}") from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
