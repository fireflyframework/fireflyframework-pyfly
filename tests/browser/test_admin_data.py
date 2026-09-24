import base64
import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("admin_path", ["/admin", "/console"])
def test_dashboard_actual_crud(tmp_path: Path, admin_path):
    from playwright.sync_api import expect, sync_playwright

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {
        **os.environ,
        "PYFLY_BROWSER_ADMIN_PATH": admin_path,
        "PYFLY_BROWSER_DATABASE": f"sqlite+aiosqlite:///{tmp_path}/browser.db",
    }
    artifacts = Path(os.environ.get("PYFLY_BROWSER_ARTIFACTS", str(tmp_path)))
    artifacts.mkdir(parents=True, exist_ok=True)
    log = (tmp_path / "server.log").open("w")
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "tests.browser.admin_app:app", "--host", "127.0.0.1", "--port", str(port)],
        env=env,
        stdout=log,
        stderr=log,
    )
    try:
        for _attempt in range(100):
            if process.poll() is not None:
                pytest.fail((tmp_path / "server.log").read_text())
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.1)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(
                extra_http_headers={
                    "Authorization": "Basic " + base64.b64encode(b"admin:browser-test-password").decode()
                }
            )
            page = context.new_page()
            page.set_default_timeout(10000)
            page.goto(f"http://127.0.0.1:{port}{admin_path}", wait_until="domcontentloaded")
            page.get_by_role("link", name="Datasources", exact=True).click()
            page.get_by_role("button", name="Products", exact=True).click()
            page.get_by_role("button", name="Create", exact=True).click()
            page.get_by_label("Name", exact=True).fill("Browser product")
            page.once("dialog", lambda dialog: dialog.dismiss())
            page.get_by_role("link", name="Datasources", exact=True).click()
            expect(page.get_by_label("Name", exact=True)).to_have_value("Browser product")
            page.get_by_label("Price", exact=True).fill("invalid-decimal")
            page.get_by_role("button", name="Save", exact=True).click()
            expect(page.get_by_role("alert")).to_contain_text("Invalid fields")
            expect(page.get_by_label("Name", exact=True)).to_have_value("Browser product")
            page.get_by_label("Price", exact=True).fill("12.50")
            page.get_by_role("button", name="Save", exact=True).click()
            expect(page.get_by_role("button", name="Browser product", exact=True)).to_be_visible()
            with sqlite3.connect(tmp_path / "browser.db") as database:
                assert database.execute("SELECT name FROM admin_test_products").fetchone()[0] == "Browser product"
            for theme in ("dark", "light"):
                page.locator("html").evaluate("(node, theme) => node.dataset.theme = theme", theme)
                page.screenshot(path=str(artifacts / f"admin-data-{theme}.png"), full_page=True, animations="disabled")
            page.get_by_role("button", name="Browser product", exact=True).click()
            page.get_by_role("button", name="Edit", exact=True).click()
            page.get_by_label("Name", exact=True).fill("My unsaved edit")
            records_url = f"http://127.0.0.1:{port}{admin_path}/api/data/resources/products/records"
            record = context.request.get(records_url).json()["items"][0]
            token = next(cookie["value"] for cookie in context.cookies() if cookie["name"] == "XSRF-TOKEN")
            updated = context.request.patch(
                records_url + "/" + record["id"],
                data={"values": {"name": "Concurrent edit"}, "editToken": record["edit_token"]},
                headers={"X-XSRF-TOKEN": token},
            )
            assert updated.status == 200
            page.get_by_role("button", name="Save", exact=True).click()
            expect(page.get_by_role("alert")).to_contain_text("has changed")
            expect(page.get_by_label("Name", exact=True)).to_have_value("My unsaved edit")
            page.once("dialog", lambda dialog: dialog.accept())
            page.get_by_role("button", name="Reload record", exact=True).click()
            expect(page.get_by_label("Name", exact=True)).to_have_value("Concurrent edit")
            page.get_by_label("Name", exact=True).fill("Changed product")
            page.get_by_role("button", name="Save", exact=True).click()
            expect(page.get_by_role("button", name="Changed product", exact=True)).to_be_visible()
            with sqlite3.connect(tmp_path / "browser.db") as database:
                assert database.execute("SELECT name FROM admin_test_products").fetchone()[0] == "Changed product"
            page.get_by_role("button", name="Changed product", exact=True).click()
            page.get_by_role("button", name="Delete", exact=True).click()
            page.get_by_role("button", name="Confirm delete", exact=True).click()
            expect(page.get_by_text("No records found.", exact=True)).to_be_visible()
            page.screenshot(path=str(artifacts / "admin-data.png"), full_page=True, animations="disabled")
            with sqlite3.connect(tmp_path / "browser.db") as database:
                assert database.execute("SELECT COUNT(*) FROM admin_test_products").fetchone()[0] == 0
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.locator("body").evaluate("(node) => node.scrollWidth <= innerWidth")
            page.screenshot(path=str(artifacts / "admin-data-mobile.png"), full_page=True, animations="disabled")
            browser.close()
    finally:
        process.terminate()
        process.wait(timeout=10)
        log.close()
