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
"""Base document with audit fields for all MongoDB documents."""

from __future__ import annotations

from datetime import UTC, datetime

from beanie import Document
from pydantic import Field


class BaseDocument(Document):
    """Base document providing audit trail fields.

    All MongoDB documents should inherit from this class to get automatic
    ``created_at`` / ``updated_at`` / ``created_by`` / ``updated_by`` tracking.

    The ``id`` field is inherited from :class:`beanie.Document` as a
    ``PydanticObjectId``. Subclasses configure their collection name via
    ``class Settings: name = "..."``.

    Example::

        class UserDocument(BaseDocument):
            name: str
            email: str

            class Settings:
                name = "users"
    """

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    created_by: str | None = None
    updated_by: str | None = None

    class Settings:
        use_state_management = True
