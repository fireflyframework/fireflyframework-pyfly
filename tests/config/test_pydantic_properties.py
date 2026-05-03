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
"""Tests for @config_properties with Pydantic BaseModel binding."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from pydantic import BaseModel, Field

from pyfly.core.config import Config, config_properties


@config_properties(prefix="myapp.database")
class DatabaseProperties(BaseModel):
    url: str = "sqlite:///test.db"
    pool_size: int = Field(default=5, ge=1, le=100)
    timeout: float = Field(default=30.0, gt=0)
    ssl: bool = False


@config_properties(prefix="myapp.server")
class ServerProperties(BaseModel):
    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)


@config_properties(prefix="myapp.nested")
class NestedProperties(BaseModel):
    class DatabaseConfig(BaseModel):
        url: str = "sqlite:///test.db"
        pool_size: int = 5

    name: str = "myapp"
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)


@config_properties(prefix="myapp.required")
class RequiredFieldProperties(BaseModel):
    name: str
    port: int = 8080


class Undecorated(BaseModel):
    value: str = "nope"


class TestPydanticBindDefaults:
    def test_empty_section_uses_defaults(self) -> None:
        config = Config({"myapp": {"database": {}}})
        props = config.bind(DatabaseProperties)
        assert props.url == "sqlite:///test.db"
        assert props.pool_size == 5
        assert props.timeout == 30.0
        assert props.ssl is False


class TestPydanticBindProvidedValues:
    def test_provided_values_override_defaults(self) -> None:
        config = Config(
            {
                "myapp": {
                    "database": {
                        "url": "postgresql://localhost/mydb",
                        "pool_size": 20,
                        "timeout": 60.0,
                        "ssl": True,
                    }
                }
            }
        )
        props = config.bind(DatabaseProperties)
        assert props.url == "postgresql://localhost/mydb"
        assert props.pool_size == 20
        assert props.timeout == 60.0
        assert props.ssl is True


class TestPydanticTypeCoercion:
    def test_string_to_int(self) -> None:
        config = Config({"myapp": {"server": {"host": "localhost", "port": "9090"}}})
        props = config.bind(ServerProperties)
        assert props.port == 9090

    def test_string_to_float(self) -> None:
        config = Config({"myapp": {"database": {"timeout": "15.5"}}})
        props = config.bind(DatabaseProperties)
        assert props.timeout == 15.5

    def test_string_to_bool(self) -> None:
        config = Config({"myapp": {"database": {"ssl": "true"}}})
        props = config.bind(DatabaseProperties)
        assert props.ssl is True


class TestPydanticValidationFailure:
    def test_pool_size_out_of_range(self) -> None:
        config = Config({"myapp": {"database": {"pool_size": 200}}})
        with pytest.raises(ValueError, match="Configuration validation failed"):
            config.bind(DatabaseProperties)

    def test_missing_required_field(self) -> None:
        config = Config({"myapp": {"required": {"port": 3000}}})
        with pytest.raises(ValueError, match="Configuration validation failed"):
            config.bind(RequiredFieldProperties)


class TestPydanticNestedModel:
    def test_nested_dict_maps_to_nested_model(self) -> None:
        config = Config(
            {
                "myapp": {
                    "nested": {
                        "name": "prod-app",
                        "database": {
                            "url": "postgresql://db/prod",
                            "pool_size": 20,
                        },
                    }
                }
            }
        )
        props = config.bind(NestedProperties)
        assert props.name == "prod-app"
        assert props.database.url == "postgresql://db/prod"
        assert props.database.pool_size == 20


class TestPydanticExtraFieldsIgnored:
    def test_extra_fields_are_ignored(self) -> None:
        config = Config({"myapp": {"server": {"host": "localhost", "port": 3000, "unknown_field": "ignored"}}})
        props = config.bind(ServerProperties)
        assert props.host == "localhost"
        assert props.port == 3000
        assert not hasattr(props, "unknown_field")


class TestDataclassBackwardCompatibility:
    def test_dataclass_binding_still_works(self) -> None:
        @config_properties(prefix="legacy")
        @dataclass
        class LegacyConfig:
            url: str = "sqlite:///legacy.db"
            pool_size: int = 5

        config = Config({"legacy": {"url": "postgresql://localhost/legacy", "pool_size": 10}})
        props = config.bind(LegacyConfig)
        assert props.url == "postgresql://localhost/legacy"
        assert props.pool_size == 10


class TestMissingConfigPropertiesDecorator:
    def test_undecorated_class_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="is not decorated with @config_properties"):
            Config({}).bind(Undecorated)


class TestPydanticPartialConfig:
    def test_partial_values_merged_with_defaults(self) -> None:
        config = Config({"myapp": {"database": {"url": "postgresql://localhost/partial"}}})
        props = config.bind(DatabaseProperties)
        assert props.url == "postgresql://localhost/partial"
        assert props.pool_size == 5
        assert props.timeout == 30.0
        assert props.ssl is False
