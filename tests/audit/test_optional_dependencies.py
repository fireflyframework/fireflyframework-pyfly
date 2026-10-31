# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Missing optional SQL packages cannot silently disable configured mandatory audit."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = r"""
import asyncio
import importlib.abc
import importlib.metadata
import sys

class NoOptionalVendors(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {"sqlalchemy", "aiosqlite", "cryptography", "pydantic_ai"}:
            raise ModuleNotFoundError("Optional dependency absent in contract", name=fullname)

sys.meta_path.insert(0, NoOptionalVendors())
from pyfly.context import ApplicationContext
from pyfly.core import Config

async def main():
    mode = sys.argv[1]
    config = Config({"pyfly": {"audit": {
        "enabled": mode in {"journal", "custom"},
        "recording": {"enabled": mode == "recording", "modules": ["data"]},
        "archive": {"enabled": mode == "archive"},
    }}})
    context = ApplicationContext(config)
    if mode == "custom":
        from pyfly.audit import AuditJournal

        class HostJournal:
            async def start(self): pass
            async def stop(self): pass
            async def append(self, event): raise AssertionError("Unexpected host adapter call")
            async def head(self, scope): raise AssertionError("Unexpected host adapter call")
            async def read(self, scope, **kwargs): raise AssertionError("Unexpected host adapter call")
            async def verify(self, scope, **kwargs): raise AssertionError("Unexpected host adapter call")
            async def export(self, scope, **kwargs): raise AssertionError("Unexpected host adapter call")

        host = HostJournal()
        context.container.register_instance(AuditJournal, host)
    try:
        if mode in {"disabled", "custom"}:
            wanted = {"audit", "audit-recording", "audit-archive"}
            found = {ep.name for ep in importlib.metadata.entry_points(group="pyfly.auto_configuration")
                     if ep.name in wanted and ep.load() is not None}
            assert found == wanted
            await context.start()
            if mode == "custom":
                assert context.get_bean(AuditJournal) is host
            assert not {"sqlalchemy", "aiosqlite", "cryptography", "pydantic_ai"}.intersection(sys.modules)
        else:
            try:
                await context.start()
            except Exception as error:
                text = str(error)
                assert "pyfly[data-relational]" in text or "AuditJournal" in text, text
            else:
                raise AssertionError("Configured mandatory audit silently disappeared during discovery")
    finally:
        await context.stop()

asyncio.run(main())
"""


@pytest.mark.parametrize("mode", ["journal", "recording", "archive", "disabled", "custom"])
def test_optional_vendor_absence_is_explicit_and_disabled_entrypoints_remain_lightweight(mode):
    environment = dict(os.environ)
    environment.pop("PYTHONHOME", None)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    completed = subprocess.run(
        [sys.executable, "-c", SCRIPT, mode],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
