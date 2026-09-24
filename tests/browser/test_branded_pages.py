import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.browser


def test_native_pages_render_assets_and_responsive_diagnostics(tmp_path):
    from playwright.sync_api import expect, sync_playwright

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    artifacts = Path(os.environ.get("PYFLY_BROWSER_ARTIFACTS", str(tmp_path)))
    artifacts.mkdir(parents=True, exist_ok=True)
    with (tmp_path / "pages.log").open("w") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "tests.browser.pages_app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            stdout=log,
            stderr=log,
        )
        try:
            for _ in range(100):
                if process.poll() is not None:
                    pytest.fail((tmp_path / "pages.log").read_text())
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    time.sleep(0.1)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page(viewport={"width": 1440, "height": 1000}, color_scheme="light")
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on(
                    "console",
                    lambda message: errors.append(message.text) if "Content Security Policy" in message.text else None,
                )
                for color in ("light", "dark"):
                    page.emulate_media(color_scheme=color)
                    response = page.goto(f"http://127.0.0.1:{port}/")
                    assert "style-src 'self'" in response.headers["content-security-policy"]
                    expect(page.get_by_role("heading", name="Welcome to PyFly")).to_be_visible()
                    assert page.locator("img").evaluate_all(
                        "images => images.every(i => i.complete && i.naturalWidth > 0)"
                    )
                    page.screenshot(path=str(artifacts / f"welcome-{color}.png"), full_page=True)
                page.emulate_media(color_scheme="light")
                response = page.goto(f"http://127.0.0.1:{port}/example-error")
                assert response.status == 500
                expect(page.get_by_role("heading", name="RuntimeError")).to_be_visible()
                expect(page.locator("pre")).to_contain_text("example_error")
                page.screenshot(path=str(artifacts / "error-debug.png"), full_page=True)
                for path, name in (("/", "welcome"), ("/example-error", "error")):
                    page.set_viewport_size({"width": 390, "height": 844})
                    page.goto(f"http://127.0.0.1:{port}{path}")
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    page.screenshot(path=str(artifacts / f"{name}-mobile.png"), full_page=True)
                assert not errors
                browser.close()
        finally:
            process.terminate()
            process.wait(timeout=10)
