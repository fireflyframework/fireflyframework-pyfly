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
"""Schema references must survive independent interpreter/allocation histories."""

import json
import os
import subprocess
import sys

from tests.web.test_openapi_contracts import assert_refs_resolve, resolve

_SOURCE = r"""
import json
import sys
from typing import Annotated, Literal
from pydantic import BaseModel, BeforeValidator, Field, create_model
from pydantic.json_schema import GenerateJsonSchema
from pyfly.web import RouteMetadata
from pyfly.web.openapi import OpenAPIGenerator

allocations = [object() for _ in range(int(sys.argv[1]))]
def text(value):
    return value

type Text = Annotated[str, BeforeValidator(text)]
type Revision = Annotated[Text, Field(pattern=r"^(?:rev:[0-9]{2}):1234567890$")]
type OtherRevision = Annotated[Text, Field(pattern=r"^(?:rev:[A-Z]{2}):1234567890$")]
type Recursive = dict[str, "Recursive"] | list["Recursive"] | Revision | int

type Wrapped[T] = list[T]

class Model_12345(BaseModel):
    value: Revision = Field(validation_alias="incoming", serialization_alias="outgoing")
    other: OtherRevision
    tree: Recursive
    literal: Literal["Text:1234567890", "https://host:9000/path"]
    documented: str = Field(default="name:1234567890", examples=["name:1234567890"],
                            description="An address-like value: 0x1234567890")

class First(BaseModel):
    kind: Literal["first"]
    payload: Model_12345

class Second(BaseModel):
    kind: Literal["second"]
    children: list["First | Second"]

type Variant = Annotated[First | Second, Field(discriminator="kind")]
type Packet = Wrapped[Variant]

Left = create_model("Collision", left=(int, ...), __module__="contracts")
Right = create_model("Collision", right=(str, ...), __module__="contracts")

class ConsumerGenerator(GenerateJsonSchema):
    calls = 0

    def get_defs_ref(self, core_mode_ref):
        ConsumerGenerator.calls += 1
        return super().get_defs_ref(core_mode_ref)

    def normalize_name(self, name):
        return "consumer_" + super().normalize_name(name)

    def chain_schema(self, schema):
        return {"allOf": [self.generate_inner(step) for step in schema["steps"]]}

metadata = [
    RouteMetadata("/packet", "POST", 200, None, "packet", request_body_model=Packet, return_type=Packet),
    RouteMetadata("/left", "GET", 200, None, "left", return_type=Left),
    RouteMetadata("/right", "GET", 200, None, "right", return_type=Right),
]
if int(sys.argv[1]):
    metadata.reverse()
spec = OpenAPIGenerator("Test", "1", schema_generator=ConsumerGenerator).generate(metadata)
assert ConsumerGenerator.calls > 0
print(json.dumps(spec, sort_keys=True, separators=(",", ":")))
"""


def _document(allocations, seed):
    environment = dict(os.environ, PYTHONHASHSEED=str(seed))
    result = subprocess.run(
        [sys.executable, "-c", _SOURCE, str(allocations)], capture_output=True, text=True, env=environment, check=True
    )
    return result.stdout


def test_component_names_are_stable_across_processes_without_losing_contracts():
    first = _document(0, 1)
    second = _document(4096, 7)
    assert first == second
    spec = json.loads(first)
    assert_refs_resolve(spec)
    schemas = spec["components"]["schemas"]
    assert any("Model_12345" in name for name in schemas)
    assert any(name.startswith("consumer_") for name in schemas)
    for path, field in (("/left", "left"), ("/right", "right")):
        response = spec["paths"][path]["get"]["responses"]["200"]
        assert field in resolve(spec, response["content"]["application/json"]["schema"])["properties"]
    for alias in ("incoming", "outgoing"):
        (model,) = [schema for schema in schemas.values() if alias in schema.get("properties", {})]
        assert model["properties"]["literal"]["enum"] == ["Text:1234567890", "https://host:9000/path"]
        assert model["properties"]["documented"] == {
            "default": "name:1234567890",
            "description": "An address-like value: 0x1234567890",
            "examples": ["name:1234567890"],
            "title": "Documented",
            "type": "string",
        }
        revision = resolve(spec, model["properties"][alias])
        assert revision["allOf"][1] == {"type": "string", "pattern": r"^(?:rev:[0-9]{2}):1234567890$"}
        other = resolve(spec, model["properties"]["other"])
        assert other["allOf"][1] == {"type": "string", "pattern": r"^(?:rev:[A-Z]{2}):1234567890$"}
        recursive = resolve(spec, model["properties"]["tree"])
        assert recursive["anyOf"][0]["additionalProperties"]["$ref"] == model["properties"]["tree"]["$ref"]
    discriminators = [schema["discriminator"] for schema in schemas.values() if "discriminator" in schema]
    assert len(discriminators) == 2
    for discriminator in discriminators:
        assert discriminator["propertyName"] == "kind"
        assert set(discriminator["mapping"]) == {"first", "second"}
