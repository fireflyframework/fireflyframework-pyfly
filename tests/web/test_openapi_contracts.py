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
"""Offline contracts are accurate without changing request dispatch or starting beans."""

import inspect
import json
import subprocess
import sys
from enum import StrEnum
from types import SimpleNamespace
from typing import Annotated, Literal
from uuid import UUID

import pytest
from pydantic import BaseModel, Field, TypeAdapter, create_model, field_serializer
from pydantic.json_schema import GenerateJsonSchema
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

import pyfly.web as web
from pyfly.container.stereotypes import rest_controller
from pyfly.web import Body, Cookie, Header, PathVar, QueryParam, get_mapping, post_mapping
from pyfly.web.adapters.starlette.app import create_app
from pyfly.web.adapters.starlette.controller import ControllerRegistrar, RouteMetadata
from pyfly.web.adapters.starlette.mounted_routes import MountedRoute
from pyfly.web.openapi import OpenAPIGenerator


class Choice(StrEnum):
    FIRST = "first"
    SECOND = "second"


class Cat(BaseModel):
    kind: Literal["cat"]
    lives: int


class Dog(BaseModel):
    kind: Literal["dog"]
    bark: bool


Pet = Annotated[Cat | Dog, Field(discriminator="kind")]


class Tree(BaseModel):
    children: list["Tree"] = []


class Aliased(BaseModel):
    value: int = Field(validation_alias="input", serialization_alias="output")

    @field_serializer("value")
    def serialize_value(self, value: int) -> str:
        return str(value)


def context(*classes):
    class Unstarted:
        container = SimpleNamespace(_registrations=dict.fromkeys(classes))

        def get_bean(self, cls):
            raise AssertionError("offline collection resolved a bean")

    return Unstarted()


def route(path="/test", name="test", **kwargs):
    return RouteMetadata(path, "POST", 200, None, name, **kwargs)


def generate(*routes, **kwargs):
    return OpenAPIGenerator("Test", "1", **kwargs).generate(list(routes))


def resolve(spec, schema):
    while "$ref" in schema:
        schema = spec["components"]["schemas"][schema["$ref"].rsplit("/", 1)[1]]
    return schema


def assert_refs_resolve(spec):
    def walk(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "$ref":
                    assert child.startswith("#/components/schemas/")
                    assert child.rsplit("/", 1)[1] in spec["components"]["schemas"]
                if key == "mapping":
                    for target in child.values():
                        assert target.rsplit("/", 1)[1] in spec["components"]["schemas"]
                        assert target.startswith("#/components/schemas/")
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(spec)


@pytest.fixture
def api():
    for name in (
        "openapi_operation",
        "OpenAPIOperation",
        "OpenAPIRequestBody",
        "OpenAPIResponse",
        "OpenAPIHeader",
        "OpenAPIParameter",
        "RouteMetadata",
    ):
        assert hasattr(web, name), f"missing public documentation API: {name}"
    return web


@rest_controller
class TypedController:
    @post_mapping("/typed/{identifier}", name="probe.typed")
    def typed(
        self,
        identifier: PathVar[UUID],
        body: Body[list[Pet]],
        count: QueryParam[Annotated[int, Field(ge=1, le=20)]] = 2,
        choice: QueryParam[Choice] = Choice.FIRST,
        token: Header[str | None] = None,
        cookie: Cookie[UUID | None] = None,
    ) -> list[Pet]:
        raise AssertionError("offline generation invoked a controller")


def test_mapping_name_and_rich_inference():
    metadata = ControllerRegistrar().collect_route_metadata(context(TypedController))
    spec = generate(*metadata)
    op = spec["paths"]["/typed/{identifier}"]["post"]
    assert op["operationId"] == "probe.typed"
    params = {p["name"]: p for p in op["parameters"]}
    assert params["identifier"]["schema"]["format"] == "uuid"
    assert params["count"]["schema"] == {"type": "integer", "minimum": 1, "maximum": 20, "default": 2}
    assert resolve(spec, params["choice"]["schema"])["enum"] == ["first", "second"]
    assert params["choice"]["schema"]["default"] == "first"
    assert params["token"]["schema"]["anyOf"] == [{"type": "string"}, {"type": "null"}]
    assert params["cookie"]["schema"]["default"] is None
    assert not params["token"]["required"]
    assert (
        op["requestBody"]["content"]["application/json"]["schema"]["items"]["discriminator"]["propertyName"] == "kind"
    )
    assert op["responses"]["200"]["content"]["application/json"]["schema"]["items"]["oneOf"]
    assert_refs_resolve(spec)
    json.dumps(spec)


def test_descriptor_inspection_is_static_and_inherited_methods_survive():
    class Trap:
        def __get__(self, obj, owner):
            raise AssertionError("descriptor evaluated")

    class Parent:
        trap = Trap()

        @get_mapping("/ordinary")
        def ordinary(self) -> str:
            return "ok"

        @staticmethod
        @get_mapping("/static")
        def static() -> str:
            return "ok"

        @classmethod
        @get_mapping("/class")
        def class_method(cls) -> str:
            return "ok"

    @rest_controller
    class Child(Parent):
        pass

    registrar = ControllerRegistrar()
    ctx = context(Child)
    assert {m.path for m in registrar.collect_route_metadata(ctx)} == {"/ordinary", "/static", "/class"}
    assert len(registrar.collect_routes(ctx)) == 3
    assert registrar.collect_websocket_routes(ctx) == []


def test_shared_definitions_modes_aliases_recursion_collisions_and_determinism():
    left = create_model("Duplicate", left=(str, ...), __module__="one")
    right = create_model("Duplicate", right=(int, ...), __module__="two")
    routes = [
        route("/alias", return_type=Aliased, request_body_model=Aliased),
        route("/left", return_type=left),
        route("/right", return_type=right),
        route("/tree", return_type=Tree),
        route("/pets", return_type=list[Pet]),
    ]
    gen = OpenAPIGenerator("Test", "1")
    spec = gen.generate(routes)
    assert gen.generate(list(reversed(routes))) == spec
    assert gen.generate(routes) == spec
    assert_refs_resolve(spec)
    op = spec["paths"]["/alias"]["post"]
    inp = resolve(spec, op["requestBody"]["content"]["application/json"]["schema"])
    out = resolve(spec, op["responses"]["200"]["content"]["application/json"]["schema"])
    assert inp["properties"]["input"]["type"] == "integer"
    assert out["properties"]["output"]["type"] == "string"
    for path, field in (("/left", "left"), ("/right", "right")):
        schema = resolve(spec, spec["paths"][path]["post"]["responses"]["200"]["content"]["application/json"]["schema"])
        assert field in schema["properties"]


def test_ids_are_unique_order_independent_and_resist_suffix_collisions():
    routes = [route("/a", "same"), route("/b", "same"), route("/c", "same_post_b")]
    mounts = [MountedRoute("/d", "POST", "same", ""), MountedRoute("/e", "POST", "same_post_d", "")]
    gen = OpenAPIGenerator("Test", "1")
    spec = gen.generate(routes, mounted_routes=mounts)
    assert gen.generate(list(reversed(routes)), mounted_routes=list(reversed(mounts))) == spec
    ids = [op["operationId"] for ops in spec["paths"].values() for op in ops.values()]
    assert len(ids) == len(set(ids))
    assert spec["paths"]["/c"]["post"]["operationId"] == "same_post_b"


def test_duplicate_controller_operations_fail():
    with pytest.raises(ValueError, match="(?i)duplicate.*POST.*/test"):
        generate(route(), route(name="other"))


@pytest.mark.parametrize("return_type", [JSONResponse, Request])
def test_unsupported_inferred_response_falls_back(return_type):
    assert generate(route(return_type=return_type))["paths"]["/test"]["post"]["responses"] == {
        "200": {"description": "Successful response"}
    }


def test_manual_contract_preserves_dispatch_and_supports_both_orders(api):
    async def manual(self, request: Request) -> JSONResponse:
        data = await request.json()
        return JSONResponse({"raw": data}, status_code=202, headers={"X-Job": "accepted"})

    signature = inspect.signature(manual)
    annotations = dict(manual.__annotations__)
    decorator = api.openapi_operation(
        operation_id="manual.accept",
        summary="Accept input",
        tags=["Manual"],
        security=[],
        request_body=api.OpenAPIRequestBody(content={"application/json": list[Pet]}, required=False),
        responses={
            202: api.OpenAPIResponse(
                "Accepted",
                content={"application/json": dict[str, object]},
                headers={"X-Job": api.OpenAPIHeader(str, description="Job state")},
            )
        },
        replace_responses=True,
    )
    assert decorator(manual) is manual
    assert inspect.signature(manual) == signature
    assert manual.__annotations__ == annotations

    @rest_controller
    class Manual:
        pass

    Manual.handle = post_mapping("/manual")(manual)
    acquisition = []
    ctx = context(Manual)
    ctx.get_bean = lambda cls: acquisition.append(cls) or cls()
    registrar = ControllerRegistrar()
    spec = generate(*registrar.collect_route_metadata(ctx))
    routes = registrar.collect_routes(ctx)
    assert acquisition == []
    response = TestClient(Starlette(routes=routes)).post("/manual", json={"not": "a pet list"})
    assert response.status_code == 202
    assert response.content == b'{"raw":{"not":"a pet list"}}'
    assert response.headers["x-job"] == "accepted"
    assert acquisition == [Manual]
    op = spec["paths"]["/manual"]["post"]
    assert set(op["responses"]) == {"202"}
    assert op["responses"]["202"]["headers"]["X-Job"]["schema"] == {"type": "string"}
    assert op["security"] == []
    assert op["operationId"] == "manual.accept"
    assert not op["requestBody"]["required"]

    @rest_controller
    class Reverse:
        @api.openapi_operation(operation_id="reverse")
        @get_mapping("/reverse", name="mapping")
        def handler(self) -> str:
            return "ok"

    assert (
        generate(*registrar.collect_route_metadata(context(Reverse)))["paths"]["/reverse"]["get"]["operationId"]
        == "reverse"
    )


def test_explicit_overrides_422_body_omission_and_empty_parameters(api):
    operation = api.OpenAPIOperation(
        request_body=None,
        parameters=[],
        tags=[],
        summary="",
        deprecated=False,
        responses={422: api.OpenAPIResponse("Actual error", content={"application/problem+json": Cat})},
    )
    spec = generate(
        route(
            request_body_model=Dog,
            operation=operation,
            summary="inferred",
            tag="tag",
            deprecated=True,
            parameters=[{"name": "q", "in": "query", "schema": {"type": "string"}}],
        )
    )
    op = spec["paths"]["/test"]["post"]
    assert "requestBody" not in op
    assert "parameters" not in op
    assert "tags" not in op
    assert "summary" not in op
    assert not op.get("deprecated", False)
    assert set(op["responses"]) == {"200", "422"}
    assert op["responses"]["422"]["description"] == "Actual error"
    assert "application/problem+json" in op["responses"]["422"]["content"]


def test_explicit_parameters_body_headers_modes_and_custom_generator(api):
    class Custom(GenerateJsonSchema):
        def generate_inner(self, schema):
            result = super().generate_inner(schema)
            if result.get("type") == "integer":
                result["x-custom"] = True
            return result

    operation = api.OpenAPIOperation(
        parameters=[
            api.OpenAPIParameter("tenant", "header", TypeAdapter(UUID), required=False),
            api.OpenAPIParameter("limit", "query", Annotated[int, Field(gt=0)], default=5),
        ],
        request_body=api.OpenAPIRequestBody(content={"application/json": TypeAdapter(Aliased)}),
        responses={
            200: api.OpenAPIResponse(
                "OK",
                content={"application/json": TypeAdapter(Aliased)},
                headers={"X-Value": api.OpenAPIHeader(Aliased)},
            )
        },
    )
    spec = generate(route(operation=operation), schema_generator=Custom)
    op = spec["paths"]["/test"]["post"]
    assert op["parameters"][0]["schema"]["format"] == "uuid"
    assert op["parameters"][1]["schema"] == {"type": "integer", "exclusiveMinimum": 0, "default": 5, "x-custom": True}
    req = resolve(spec, op["requestBody"]["content"]["application/json"]["schema"])
    head = resolve(spec, op["responses"]["200"]["headers"]["X-Value"]["schema"])
    assert req["properties"]["input"]["x-custom"] is True
    assert head["properties"]["output"]["type"] == "string"
    assert_refs_resolve(spec)


def test_security_and_generator_injection(api):
    gen = OpenAPIGenerator(
        "Custom", "2", security_schemes={"token": {"type": "http", "scheme": "bearer"}}, security=[{"token": []}]
    )
    client = TestClient(create_app(openapi_generator=gen))
    spec = client.get("/openapi.json").json()
    assert spec["security"] == [{"token": []}]
    assert spec["components"]["securitySchemes"]["token"]["scheme"] == "bearer"
    assert spec["info"]["title"] == "Custom"


def test_duplicate_explicit_ids_fail_and_implicit_id_yields(api):
    explicit = api.OpenAPIOperation(operation_id="chosen")
    with pytest.raises(ValueError, match="(?i)duplicate.*operation.*chosen"):
        generate(route("/one", operation=explicit), route("/two", operation=explicit))
    spec = generate(route("/one", operation=explicit), route("/two", "chosen"))
    assert spec["paths"]["/one"]["post"]["operationId"] == "chosen"
    assert spec["paths"]["/two"]["post"]["operationId"] != "chosen"


@pytest.mark.parametrize("kind", ["body", "response", "parameter", "header"])
def test_invalid_explicit_schema_fails_clearly(api, kind):
    if kind == "body":
        op = api.OpenAPIOperation(request_body=api.OpenAPIRequestBody(content={"application/json": Request}))
    elif kind == "response":
        op = api.OpenAPIOperation(responses={200: api.OpenAPIResponse("OK", content={"application/json": Request})})
    elif kind == "parameter":
        op = api.OpenAPIOperation(parameters=[api.OpenAPIParameter("q", "query", Request)])
    else:
        op = api.OpenAPIOperation(responses={200: api.OpenAPIResponse("OK", headers={"X": api.OpenAPIHeader(Request)})})
    with pytest.raises(ValueError, match="(?i)schema.*POST /test"):
        generate(route(operation=op))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"responses": {99: "bad"}},
        {"responses": {200: "bad"}},
        {"responses": {"200": None}},
        {"request_body": {}},
        {"parameters": ["q"]},
        {"operation_id": ""},
        {"security": ["bad"]},
        {"replace_responses": True, "responses": {}},
    ],
)
def test_malformed_operation_metadata_fails(api, kwargs):
    with pytest.raises((ValueError, TypeError)):
        generate(route(operation=api.OpenAPIOperation(**kwargs)))


def test_validation_schema_names_cannot_overwrite_user_models():
    model = create_model("HTTPValidationError", custom=(str, ...))
    spec = generate(route(return_type=model, request_body_model=Cat))
    op = spec["paths"]["/test"]["post"]
    success = resolve(spec, op["responses"]["200"]["content"]["application/json"]["schema"])
    failure = resolve(spec, op["responses"]["422"]["content"]["application/json"]["schema"])
    assert "custom" in success["properties"]
    assert "detail" in failure["properties"]
    assert_refs_resolve(spec)


def test_offline_public_import_and_generation_do_not_import_starlette(api):
    source = """
import sys
class BlockStarlette:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "starlette" or fullname.startswith("starlette."):
            raise AssertionError("offline contract imported Starlette: " + fullname)
sys.meta_path.insert(0, BlockStarlette())
from pyfly.web import RouteMetadata, OpenAPIOperation, OpenAPIResponse
from pyfly.web.openapi import OpenAPIGenerator
operation = OpenAPIOperation(responses={200: OpenAPIResponse("OK", content={"application/json": int})})
route = RouteMetadata("/offline", "GET", 200, None, "offline", operation=operation)
spec = OpenAPIGenerator("Offline", "1").generate([route])
response = spec["paths"]["/offline"]["get"]["responses"]["200"]
assert response["content"]["application/json"]["schema"]["type"] == "integer"
"""
    result = subprocess.run([sys.executable, "-c", source], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "factory,kwargs",
    [
        ("OpenAPIRequestBody", {"content": {"application/json": str}, "required": "yes"}),
        ("OpenAPIRequestBody", {"content": {"application/json": str}, "description": 42}),
        ("OpenAPIParameter", {"name": "q", "location": "query", "schema": str, "required": "yes"}),
        ("OpenAPIHeader", {"schema": str, "description": 42}),
        ("OpenAPIHeader", {"schema": str, "required": "yes"}),
    ],
)
def test_malformed_contract_values_fail(api, factory, kwargs):
    with pytest.raises((TypeError, ValueError)):
        getattr(api, factory)(**kwargs)


def test_unresolved_explicit_type_has_route_context(api):
    operation = api.OpenAPIOperation(
        responses={200: api.OpenAPIResponse("OK", content={"application/json": "MissingType"})}
    )
    with pytest.raises(ValueError, match="(?i)schema.*POST /test"):
        generate(route(operation=operation))


def test_explicit_422_replaces_inferred_validation(api):
    operation = api.OpenAPIOperation(responses={422: api.OpenAPIResponse("Bad input", content={"text/plain": str})})
    spec = generate(route(request_body_model=Cat, operation=operation))
    assert spec["paths"]["/test"]["post"]["responses"]["422"] == {
        "description": "Bad input",
        "content": {"text/plain": {"schema": {"type": "string"}}},
    }
    assert "HTTPValidationError" not in spec["components"]["schemas"]


def test_same_module_model_names_and_recursive_schema_are_distinct():
    left = create_model("Twin", left=(str, ...))
    right = create_model("Twin", right=(int, ...))
    spec = generate(
        route("/left", return_type=left), route("/right", return_type=right), route("/tree", return_type=Tree)
    )
    for path, field in (("/left", "left"), ("/right", "right"), ("/tree", "children")):
        schema = resolve(spec, spec["paths"][path]["post"]["responses"]["200"]["content"]["application/json"]["schema"])
        assert field in schema["properties"]
    assert_refs_resolve(spec)


def test_optional_parameter_without_default_is_not_required():
    @rest_controller
    class OptionalController:
        @get_mapping("/optional")
        def handle(self, value: QueryParam[int | None]) -> str:
            return "ok"

    spec = generate(*ControllerRegistrar().collect_route_metadata(context(OptionalController)))
    param = spec["paths"]["/optional"]["get"]["parameters"][0]
    assert param["required"] is False
    assert "default" not in param["schema"]
    assert param["schema"]["anyOf"] == [{"type": "integer"}, {"type": "null"}]


def test_security_document_mutation_does_not_leak_to_next_generation():
    generator = OpenAPIGenerator("Test", "1", security=[{"token": ["read"]}])
    spec = generator.generate()
    spec["security"][0]["token"].append("write")
    assert generator.generate()["security"] == [{"token": ["read"]}]


def test_non_sequence_tags_are_rejected(api):
    with pytest.raises(TypeError, match="tags"):
        api.OpenAPIOperation(tags={"tag": "wrong shape"})


def test_custom_chain_generator_applies_to_request_and_response(api):
    from pydantic_core import core_schema

    class Chained:
        @classmethod
        def __get_pydantic_core_schema__(cls, source, handler):
            return core_schema.chain_schema(
                [core_schema.str_schema(min_length=3), core_schema.str_schema(max_length=10)]
            )

    class ChainGenerator(GenerateJsonSchema):
        def chain_schema(self, schema):
            return {"allOf": [self.generate_inner(step) for step in schema["steps"]]}

    operation = api.OpenAPIOperation(
        request_body=api.OpenAPIRequestBody(content={"application/json": Chained}),
        responses={200: api.OpenAPIResponse("OK", content={"application/json": Chained})},
    )
    spec = generate(route(operation=operation), schema_generator=ChainGenerator)
    op = spec["paths"]["/test"]["post"]
    expected = {"allOf": [{"type": "string", "minLength": 3}, {"type": "string", "maxLength": 10}]}
    assert op["requestBody"]["content"]["application/json"]["schema"] == expected
    assert op["responses"]["200"]["content"]["application/json"]["schema"] == expected


def test_nullable_body_default_does_not_invent_optional_runtime_binding():
    @rest_controller
    class NullableBodyController:
        @post_mapping("/nullable")
        def handle(self, body: Body[Cat | None] = None) -> str:
            return "ok"

    spec = generate(*ControllerRegistrar().collect_route_metadata(context(NullableBodyController)))
    body = spec["paths"]["/nullable"]["post"]["requestBody"]
    assert body["required"] is True
    assert {"type": "null"} in body["content"]["application/json"]["schema"]["anyOf"]


@pytest.mark.parametrize(
    "binding,location,required",
    [(QueryParam, "query", False), (Header, "header", False), (Cookie, "cookie", False), (PathVar, "path", True)],
)
async def test_none_only_parameters_match_omission_behavior(binding, location, required):
    from pyfly.web.adapters.starlette.resolver import ParameterResolver

    path = "/none/{value}" if location == "path" else "/none"

    @rest_controller
    class NoneOnlyController:
        @get_mapping(path)
        def handle(self, value: binding[type(None)]) -> str:
            return "ok"

    spec = generate(*ControllerRegistrar().collect_route_metadata(context(NoneOnlyController)))
    param = spec["paths"][path]["get"]["parameters"][0]
    assert param["in"] == location
    assert param["schema"] == {"type": "null"}
    assert param["required"] is required

    resolver = ParameterResolver(NoneOnlyController.handle)
    request = Request({"type": "http", "headers": [], "query_string": b"", "path_params": {}})
    if required:
        with pytest.raises(ValueError, match="Missing path variable"):
            await resolver.resolve(request)
    else:
        assert await resolver.resolve(request) == {"value": None}
