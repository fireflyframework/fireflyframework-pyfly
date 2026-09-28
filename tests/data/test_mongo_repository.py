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
"""Tests for MongoRepository that need no database: its generic declaration and construction.

The repository's database behavior is proven on a real MongoDB replica set in
``tests/integration/test_mongo_repository.py`` and the other ``tests/integration/test_mongo_*`` modules.
"""

from __future__ import annotations

import pytest

from pyfly.data.document.mongodb.document import BaseDocument
from pyfly.data.document.mongodb.repository import MongoRepository

# ---------------------------------------------------------------------------
# Test document
# ---------------------------------------------------------------------------


class SampleItem(BaseDocument):
    name: str
    description: str = ""
    active: bool = True

    class Settings:
        name = "test_items"


# ===========================================================================
# Declaration
# ===========================================================================


class TestInitSubclass:
    """Tests for __init_subclass__ entity type extraction."""

    def test_extracts_entity_type(self):
        class SampleItemRepo(MongoRepository[SampleItem, str]):
            pass

        assert SampleItemRepo._entity_type is SampleItem
        assert SampleItemRepo._id_type is str

    def test_unparameterized_subclass_has_none(self):
        class BaseRepo(MongoRepository):
            pass

        assert BaseRepo._entity_type is None
        assert BaseRepo._id_type is None

    def test_optional_model_uses_entity_type(self):
        class SampleItemRepo(MongoRepository[SampleItem, str]):
            pass

        repo = SampleItemRepo()
        assert repo._model is SampleItem

    def test_explicit_model_takes_precedence(self):
        class SampleItemRepo(MongoRepository[SampleItem, str]):
            pass

        class OtherDoc(BaseDocument):
            name: str

            class Settings:
                name = "other"

        repo = SampleItemRepo(model=OtherDoc)
        assert repo._model is OtherDoc

    def test_no_model_no_generic_raises(self):
        class BareRepo(MongoRepository):
            pass

        with pytest.raises(TypeError, match="requires either"):
            BareRepo()
