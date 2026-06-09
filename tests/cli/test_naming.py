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
"""Tests for the generator naming helper."""

from __future__ import annotations

from pyfly.cli.naming import names


class TestNames:
    def test_pascal_from_kebab(self) -> None:
        n = names("user-account")
        assert n.pascal == "UserAccount"
        assert n.snake == "user_account"
        assert n.kebab == "user-account"
        assert n.camel == "userAccount"

    def test_from_pascal_input(self) -> None:
        n = names("UserAccount")
        assert n.snake == "user_account"
        assert n.kebab == "user-account"

    def test_from_snake_input(self) -> None:
        n = names("order_item")
        assert n.pascal == "OrderItem"

    def test_plurals(self) -> None:
        assert names("category").snake_plural == "categories"
        assert names("box").snake_plural == "boxes"
        assert names("user").snake_plural == "users"
        assert names("Category").kebab_plural == "categories"
        assert names("User").pascal_plural == "Users"

    def test_human(self) -> None:
        assert names("user-account").human == "user account"
