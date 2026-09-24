import pytest
from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.testclient import TestClient


class PersonForm(BaseModel):
    name: str
    active: bool = False
    tags: list[str] = []


@pytest.fixture(params=["starlette", "fastapi"])
async def client(request):
    from importlib import import_module

    from pyfly.container import controller
    from pyfly.context.application_context import ApplicationContext
    from pyfly.core.config import Config
    from pyfly.web import File, Form, UploadedFile, get_mapping, post_mapping

    @controller
    class Forms:
        @get_mapping("/form")
        async def get(self, request: Request) -> HTMLResponse:
            return HTMLResponse(request.state.csrf_token)

        @post_mapping("/form")
        async def post(self, person: Form[PersonForm]) -> dict:
            return person.model_dump()

        @post_mapping("/upload")
        async def upload(self, person: Form[PersonForm], attachment: File[UploadedFile]) -> dict:
            return {"name": person.name, "file": (await attachment.read()).decode()}

    ctx = ApplicationContext(Config({"pyfly": {"security": {"csrf": {"cookie-secure": False}}}}))
    ctx.register_bean(Forms)
    await ctx.start()
    try:
        app = import_module(f"pyfly.web.adapters.{request.param}.app").create_app(context=ctx, docs_enabled=False)
        with TestClient(app, raise_server_exceptions=False) as result:
            yield result
    finally:
        await ctx.stop()


def test_plain_form_and_stable_csrf(client):
    first = client.get("/form")
    assert first.status_code == 200
    token = first.text
    assert token == client.cookies["XSRF-TOKEN"]
    assert client.get("/form").text == token
    response = client.post("/form", data={"name": "Ada", "active": "false", "tags": ["a", "b"], "_csrf": token})
    assert response.status_code == 200, response.text
    assert response.json() == {"name": "Ada", "active": False, "tags": ["a", "b"]}
    assert client.post("/form", data={"name": "Forgery"}).status_code == 403


def test_repeated_scalar_rejected(client):
    token = client.get("/form").text
    response = client.post("/form", data={"name": ["Ada", "Bob"], "_csrf": token})
    assert response.status_code == 422


def test_oversized_csrf_form_is_413(client):
    client.get("/form")
    response = client.post("/form", data={"name": "x" * (1024 * 1024 + 1)})
    assert response.status_code == 413


def test_csrf_rejection_negotiates_html(client):
    from pyfly.web.templating.config import HtmlErrorProperties

    client.app.state.pyfly_html_errors = HtmlErrorProperties(html_enabled=True)
    client.get("/form")
    response = client.post("/form", data={"name": "x"}, headers={"Accept": "text/html"})
    assert response.status_code == 403
    assert response.headers["content-type"].startswith("text/html")


async def test_csrf_rotates_when_session_identity_changes():
    from starlette.requests import Request
    from starlette.responses import Response

    from pyfly.session.session import HttpSession
    from pyfly.web.adapters.starlette.filters.csrf_filter import CsrfFilter

    token = "existing-token"
    request = Request(
        {"type": "http", "method": "GET", "path": "/", "headers": [(b"cookie", f"XSRF-TOKEN={token}".encode())]}
    )
    request.state.session = HttpSession("old-session")

    async def login(req):
        req.state.session.rotate_id()
        return Response(status_code=303)

    response = await CsrfFilter(cookie_secure=False).do_filter(request, login)
    assert f"XSRF-TOKEN={token};" not in response.headers["set-cookie"]


def test_multipart_form_and_upload_share_csrf_body(client):
    token = client.get("/form").text
    response = client.post(
        "/upload", data={"name": "Ada", "_csrf": token}, files={"attachment": ("hello.txt", b"Hello", "text/plain")}
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"name": "Ada", "file": "Hello"}


async def test_validation_values_redact_sensitive_fields():
    from urllib.parse import urlencode

    from pydantic import Field, SecretStr

    from pyfly.web.adapters.starlette.form_binding import bind_form
    from pyfly.web.forms import FormValidationException

    class Credentials(BaseModel):
        username: str = Field(min_length=4, alias="user")
        password: SecretStr
        api_token: str

    body = urlencode({"user": "a", "password": "PRIVATE", "api_token": "TOKEN"}).encode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request(
        {"type": "http", "method": "POST", "headers": [(b"content-type", b"application/x-www-form-urlencoded")]},
        receive,
    )
    with pytest.raises(FormValidationException) as caught:
        await bind_form(request, "credentials", Credentials, None, False)
    assert caught.value.values == {"user": "a"}
    assert "PRIVATE" not in repr(caught.value.errors) and "TOKEN" not in repr(caught.value.errors)


async def test_password_alias_is_not_redisplayed():
    from pydantic import Field

    from pyfly.web.adapters.starlette.form_binding import bind_form
    from pyfly.web.forms import FormValidationException

    class Login(BaseModel):
        password: str = Field(alias="p")
        age: int

    async def receive():
        return {"type": "http.request", "body": b"p=SUPERSECRET&age=oops", "more_body": False}

    request = Request(
        {"type": "http", "method": "POST", "headers": [(b"content-type", b"application/x-www-form-urlencoded")]},
        receive,
    )
    with pytest.raises(FormValidationException) as caught:
        await bind_form(request, "login", Login, None, False)
    assert "p" not in caught.value.values


def test_every_repeated_file_obeys_size_limit(client):
    from pyfly.core.config import Config

    token = client.get("/form").text
    client.app.state.pyfly_config = Config({"pyfly": {"web": {"forms": {"max-part-size": 3}}}})
    response = client.post(
        "/upload",
        data={"name": "Ada"},
        headers={"X-XSRF-TOKEN": token},
        files=[
            ("attachment", ("big.txt", b"oversized", "text/plain")),
            ("attachment", ("small.txt", b"x", "text/plain")),
        ],
    )
    assert response.status_code == 413
