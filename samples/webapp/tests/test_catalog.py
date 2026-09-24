import re
from decimal import Decimal

from sqlalchemy import select
from starlette.testclient import TestClient


def test_native_pages_forms_and_admin_share_models(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBAPP_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/catalog.db")
    monkeypatch.setenv("WEBAPP_ADMIN_PASSWORD", "test-browser-password")
    monkeypatch.setenv("WEBAPP_EDIT_TOKEN_KEY", "test-edit-token-key-of-at-least-32-bytes")
    monkeypatch.setenv("PYFLY_SECURITY_CSRF_COOKIE_SECURE", "false")
    from catalog.app import create_webapp
    from catalog.models import Product

    app = create_webapp(initialize_schema=True)
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/admin/api/data/sources").status_code == 401
        client.auth = ("admin", "test-browser-password")
        response = client.get("/products/new")
        assert response.status_code == 200
        csrf = re.search(r'name="_csrf" value="([^"]+)"', response.text).group(1)
        invalid = client.post("/products/new", data={"name": "", "price": "-1", "_csrf": csrf})
        assert invalid.status_code == 422 and "Invalid value" in invalid.text
        response = client.post(
            "/products/new", data={"name": "Notebook", "price": "5.50", "_csrf": csrf}, follow_redirects=False
        )
        assert response.status_code == 303
        assert "Notebook" in client.get("/").text

        async def saved():
            async with app.state.catalog_factory() as session:
                return (await session.scalars(select(Product))).one()

        product = client.portal.call(saved)
        assert product.name == "Notebook" and product.price == Decimal("5.50")
        records = client.get("/admin/api/data/resources/products/records").json()
        assert records["items"][0]["id"] == str(product.id)
        response = client.get(f"/products/{product.id}/edit")
        edit_version = re.search(r'name="edit_version" value="([^"]+)"', response.text).group(1)
        response = client.post(
            f"/products/{product.id}/edit",
            data={"name": "Updated", "price": "7.00", "_csrf": csrf, "edit_version": edit_version},
        )
        assert response.status_code == 200 and "Updated" in response.text
        assert client.portal.call(saved).name == "Updated"
        assert "Page not found" in client.get("/missing", headers={"Accept": "text/html"}).text
        assert client.get("/static/catalog.css").status_code == 200
