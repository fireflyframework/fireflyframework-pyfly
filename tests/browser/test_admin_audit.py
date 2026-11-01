# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Real native journal pages, range verification and keyboard/mobile read-only UX."""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from pyfly.security.jwt import JWTService

pytestmark = pytest.mark.browser


def test_audit_dashboard_browser(tmp_path):
    from playwright.sync_api import expect, sync_playwright

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    artifacts = Path(os.environ.get("PYFLY_BROWSER_ARTIFACTS", str(tmp_path)))
    artifacts.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ, PYFLY_AUDIT_BROWSER_DB=str(tmp_path / "audit.db"))
    token = JWTService("test-jwt-signing-key-long-enough-for-hs256").encode(
        {"sub": "verified-reader", "roles": ["ADMIN"]}
    )
    with (tmp_path / "server.log").open("w") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "tests.browser.audit_admin_app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            env=environment,
            stdout=log,
            stderr=log,
        )
        try:
            for _ in range(100):
                if process.poll() is not None:
                    pytest.fail((tmp_path / "server.log").read_text())
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    time.sleep(0.1)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page(
                    viewport={"width": 1440, "height": 1000},
                    extra_http_headers={"Authorization": "Bearer " + token},
                    reduced_motion="reduce",
                )
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{port}/admin#audit")
                expect(page.get_by_role("heading", name="Audit journal", exact=True)).to_be_visible()
                expect(page.get_by_role("link", name="Audit journal", exact=True)).to_be_visible()
                expect(page.get_by_text("approved-project", exact=True)).to_be_visible()
                expect(page.get_by_role("button", name="Entry 1", exact=True)).to_be_visible()
                page.get_by_role("button", name="Entry 1", exact=True).focus()
                page.keyboard.press("Enter")
                expect(page.get_by_role("region", name="Audit entry details")).to_contain_text("research-agent-0")
                page.get_by_role("button", name="Verify displayed range", exact=True).click()
                expect(page.get_by_role("status")).to_contain_text(
                    "Range verified. No independent anchor was supplied."
                )
                page.get_by_role("button", name="Next page", exact=True).click()
                expect(page.get_by_role("button", name="Entry 26", exact=True)).to_be_visible()
                expect(page.get_by_role("button", name="Next page", exact=True)).to_be_disabled()
                for theme in ("dark", "light"):
                    page.locator("html").evaluate("(node, theme) => node.dataset.theme = theme", theme)
                    page.screenshot(path=str(artifacts / f"admin-audit-{theme}.png"), full_page=True)
                for width in (1024, 390):
                    page.set_viewport_size({"width": width, "height": 844})
                    if width == 390:
                        page.wait_for_function(
                            "document.querySelector('.admin-sidebar').getBoundingClientRect().right <= 0"
                        )
                    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                    expect(page.get_by_role("button", name="Verify displayed range", exact=True)).to_be_visible()
                    page.screenshot(
                        path=str(artifacts / f"admin-audit-{width}.png"), full_page=True, animations="disabled"
                    )
                page.get_by_role("button", name="Entry 26", exact=True).click()
                expect(page.get_by_role("region", name="Audit entry details")).to_contain_text("research-agent-25")
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                page.screenshot(
                    path=str(artifacts / "admin-audit-390-details.png"), full_page=True, animations="disabled"
                )
                page.route(
                    "**/api/audit?*",
                    lambda route: route.fulfill(status=403, json={"error": "Audit access is forbidden"}),
                )
                page.get_by_role("button", name="Refresh", exact=True).click()
                expect(page.get_by_role("alert")).to_contain_text("Audit access is forbidden")
                expect(page.get_by_role("button", name="Entry 26", exact=True)).to_have_count(0)
                assert not errors
                browser.close()
        finally:
            process.terminate()
            process.wait(timeout=10)
