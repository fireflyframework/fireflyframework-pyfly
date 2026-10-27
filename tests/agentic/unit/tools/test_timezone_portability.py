# Copyright 2026 Firefly Software Foundation & Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Named native timezones work without an operating-system zoneinfo database."""

import os
import subprocess
import sys
from pathlib import Path


def test_datetime_tool_resolves_iana_alias_without_system_timezone_files():
    root = Path(__file__).resolve().parents[4]
    code = """
import asyncio,re
from pyfly.agentic.tools.builtins.datetime_tool import DateTimeTool
async def main():
    value = await DateTimeTool().execute(action='date', timezone='US/Eastern')
    assert re.fullmatch(r'\\d{4}-\\d{2}-\\d{2}', value)
asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=root,
        env={**os.environ, "PYTHONTZPATH": "", "PYTHONPATH": str(root / "src")},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
