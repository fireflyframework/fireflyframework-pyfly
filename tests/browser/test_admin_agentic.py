"""Actual browser coverage of native discovery, brand assets and view states."""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.browser


def test_agentic_dashboard_browser(tmp_path):
    from playwright.sync_api import expect, sync_playwright

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    artifacts = Path(os.environ.get("PYFLY_BROWSER_ARTIFACTS", str(tmp_path)))
    artifacts.mkdir(parents=True, exist_ok=True)
    with (tmp_path / "server.log").open("w") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "tests.browser.agentic_admin_app:app",
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
                    pytest.fail((tmp_path / "server.log").read_text())
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    time.sleep(0.1)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page(viewport={"width": 1440, "height": 1050}, reduced_motion="reduce")
                page.set_default_timeout(10000)
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{port}/admin#agentic")
                expect(page).to_have_title("pyfly admin")
                expect(page.locator(".admin-navbar-title")).to_have_text("pyfly admin")
                expect(page.get_by_role("heading", name="Agentic", exact=True)).to_be_visible()
                expect(page.get_by_role("link", name="Agentic", exact=True)).to_be_visible()
                expect(page.get_by_role("button", name="research_assistant", exact=True)).to_be_visible()
                page.get_by_role("button", name="research_assistant", exact=True).focus()
                page.keyboard.press("Enter")
                expect(page.get_by_role("region", name="Agent details")).to_contain_text("research_assistant")
                assert (
                    page.locator("html").evaluate(
                        "node => getComputedStyle(node).getPropertyValue('--admin-primary').trim()"
                    )
                    == "#ffb34a"
                )
                assert page.locator("body").evaluate("node => getComputedStyle(node).fontFamily").startswith("Manrope")
                assert page.evaluate(
                    'async () => { await document.fonts.ready; return document.fonts.check("14px Manrope"); }'
                )
                assert page.locator(".admin-sidebar-brand img").evaluate("node => node.naturalWidth > 0")
                page.screenshot(
                    path=str(artifacts / "admin-agentic-agent-details.png"), full_page=True, animations="disabled"
                )
                for theme in ("dark", "light"):
                    page.locator("html").evaluate("(node, theme) => node.dataset.theme = theme", theme)
                    page.locator(".admin-content").evaluate("node => node.scrollTop = 0")
                    page.screenshot(
                        path=str(artifacts / f"admin-agentic-{theme}.png"), full_page=True, animations="disabled"
                    )
                page.get_by_role("button", name="knowledge_lookup", exact=True).click()
                expect(page.get_by_role("region", name="Tool details")).to_contain_text("Parameters")
                page.set_viewport_size({"width": 390, "height": 844})
                page.locator(".admin-content").evaluate("node => node.scrollTop = 0")
                assert page.locator("body").evaluate("node => node.scrollWidth <= innerWidth")
                page.screenshot(path=str(artifacts / "admin-agentic-mobile.png"), full_page=True, animations="disabled")
                page.get_by_role("button", name="Toggle navigation", exact=True).click()
                expect(page.get_by_role("link", name="Agentic", exact=True)).to_be_visible()
                page.keyboard.press("Escape")
                expect(page.get_by_role("button", name="Toggle navigation", exact=True)).to_be_focused()
                page.set_viewport_size({"width": 720, "height": 525})
                assert page.locator("body").evaluate("node => node.scrollWidth <= innerWidth")
                page.screenshot(
                    path=str(artifacts / "admin-agentic-200-percent-equivalent.png"),
                    full_page=True,
                    animations="disabled",
                )
                page.set_viewport_size({"width": 1440, "height": 1050})
                empty = {"available": True, "agents": [], "tools": [], "workflows": [], "usage": {}, "quota": {}}
                page.route("**/api/agentic", lambda route: route.fulfill(json=empty))
                page.get_by_role("button", name="Refresh", exact=True).click()
                expect(page.get_by_text("No agents registered", exact=True)).to_be_visible()
                page.screenshot(path=str(artifacts / "admin-agentic-empty.png"), full_page=True, animations="disabled")
                page.unroute("**/api/agentic")
                page.route("**/api/agentic", lambda route: route.fulfill(status=503, json={"error": "Unavailable"}))
                page.get_by_role("button", name="Refresh", exact=True).click()
                expect(page.get_by_role("alert")).to_contain_text("Could not load Agentic data")
                page.screenshot(
                    path=str(artifacts / "admin-agentic-failure.png"), full_page=True, animations="disabled"
                )
                page.unroute("**/api/agentic")
                page.get_by_role("button", name="Try again", exact=True).click()
                expect(page.get_by_role("button", name="research_assistant", exact=True)).to_be_visible()
                pending = []
                page.route("**/api/agentic", lambda route: pending.append(route))
                page.get_by_role("button", name="Refresh", exact=True).click()
                expect(page.get_by_role("status", name="Loading Agentic data")).to_be_visible()
                page.screenshot(path=str(artifacts / "admin-agentic-loading.png"), animations="disabled")
                pending[0].continue_()
                page.unroute("**/api/agentic")
                expect(page.get_by_role("button", name="research_assistant", exact=True)).to_be_visible()
                page.keyboard.press("Control+k")
                page.get_by_role("textbox", name="Search", exact=True).fill("Agentic")
                page.keyboard.press("Enter")
                expect(page.get_by_role("heading", name="Agentic", exact=True)).to_be_visible()
                for route in (
                    "",
                    "beans",
                    "health",
                    "env",
                    "config",
                    "loggers",
                    "metrics",
                    "scheduled",
                    "mappings",
                    "caches",
                    "flags",
                    "cqrs",
                    "transactions",
                    "traces",
                    "logfile",
                    "runtime",
                    "observability",
                ):
                    page.goto(f"http://127.0.0.1:{port}/admin#{route}")
                    expect(page.locator("#content .loading-spinner")).to_have_count(0)
                    expect(page.locator("#content .skeleton")).to_have_count(0)
                    expect(page.locator("#content")).not_to_contain_text("Failed to load view")
                    if route in ("", "config", "beans"):
                        page.evaluate("""() => {
                            if (window.Chart) for (const chart of Object.values(Chart.instances)) {
                                chart.stop(); chart.update('none');
                            }
                        }""")
                        page.screenshot(
                            path=str(artifacts / f"admin-{route or 'overview'}-brand.png"), animations="disabled"
                        )
                page.route(
                    "**/api/settings",
                    lambda route: route.fulfill(
                        json={
                            "title": "pyfly admin",
                            "theme": "dark",
                            "refreshInterval": 5000,
                            "serverMode": False,
                            "dataEnabled": False,
                            "agenticEnabled": False,
                        }
                    ),
                )
                page.goto(f"http://127.0.0.1:{port}/admin")
                expect(page.get_by_role("link", name="Beans", exact=True)).to_be_visible()
                expect(page.get_by_role("link", name="Agentic", exact=True)).to_have_count(0)
                assert errors == []
                browser.close()
        finally:
            process.terminate()
            process.wait(timeout=10)
