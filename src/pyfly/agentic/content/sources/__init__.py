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

"""Content sources: external systems that yield files for ingestion.

ContentSource preserves the v1 local-Path contract. ContentSourceV2 adds
stateless ordered change pages and bounded streaming for delegated cloud sources.
Applications own durable checkpoints, OAuth callback identity and reader fences.
"""

from __future__ import annotations

from pyfly.agentic.content.sources.base import ContentSource, RawFile
from pyfly.agentic.content.sources.dtos import (
    Account,
    Change,
    ChangePage,
    Cursor,
    DownloadReceipt,
    ItemPage,
    RemoteItem,
    SourceScope,
)
from pyfly.agentic.content.sources.errors import SourceError
from pyfly.agentic.content.sources.google_drive import GoogleDriveSource
from pyfly.agentic.content.sources.local_folder import (
    LocalFolderSource,
    LocalFolderSourceConfig,
)
from pyfly.agentic.content.sources.microsoft import OneDriveSource, SharePointSource
from pyfly.agentic.content.sources.oauth import ReadOnlySourceOAuth, SourceOAuthConfig
from pyfly.agentic.content.sources.ports import AsyncByteSink, ContentSourceV2, DelegatedTokenSource

__all__ = [
    "Account",
    "AsyncByteSink",
    "Change",
    "ChangePage",
    "ContentSourceV2",
    "Cursor",
    "DelegatedTokenSource",
    "DownloadReceipt",
    "GoogleDriveSource",
    "ItemPage",
    "OneDriveSource",
    "ReadOnlySourceOAuth",
    "RemoteItem",
    "SharePointSource",
    "SourceError",
    "SourceOAuthConfig",
    "SourceScope",
    "ContentSource",
    "LocalFolderSource",
    "LocalFolderSourceConfig",
    "RawFile",
]
