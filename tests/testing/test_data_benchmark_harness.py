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
"""The data-layer benchmark harness (``benchmarks/data/``) keeps measuring while the data layer changes.

The redesign packages re-run the harness and compare with ``BASELINE.md``, so the harness must reach
the framework only through public entry points, and one scenario that breaks must not hide the others.
The harness runs in a separate process on the sqlite-file lane: its benchmark model and beans stay out
of this test run's ``Base.metadata`` and container.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HARNESS = _REPO_ROOT / "benchmarks" / "data"


@dataclass(frozen=True)
class HarnessRun:
    """One ``benchmarks/data/run.py`` process: its exit code, its output and its ``--json`` report."""

    returncode: int
    output: str
    report: dict[str, Any] | None


@pytest.fixture(scope="module")
def failing_then_derived(tmp_path_factory: pytest.TempPathFactory) -> HarnessRun:
    """The harness run with a scenario that raises, followed by ``derived``, on sqlite-file."""
    report_path = tmp_path_factory.mktemp("harness") / "report.json"
    driver = textwrap.dedent(
        f"""
        import sys

        from benchmarks.data import run, scenarios


        async def boom(env):
            raise RuntimeError("boom")


        scenarios.SCENARIOS["boom"] = boom
        sys.exit(run.main(["--scenario", "boom", "derived", "--json", {str(report_path)!r}]))
        """
    )
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [str(_REPO_ROOT), os.environ.get("PYTHONPATH")]))}
    result = subprocess.run(
        [sys.executable, "-c", driver],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    report = json.loads(report_path.read_text()) if report_path.exists() else None
    return HarnessRun(result.returncode, result.stdout + result.stderr, report)


def test_a_failing_scenario_is_recorded_and_the_run_goes_on(failing_then_derived: HarnessRun) -> None:
    run = failing_then_derived
    assert run.report is not None, f"no --json report was written:\n{run.output}"
    assert run.report["scenarios"]["boom"] == {"error": "RuntimeError('boom')"}
    assert "error" not in run.report["scenarios"]["derived"], run.report["scenarios"]["derived"]
    assert run.report["meta"]["backend"] == "sqlite-file"
    assert run.returncode == 1, "a run with a failed scenario must not exit 0"
    assert "boom" in run.output


def test_derived_times_the_finder_and_its_hand_written_twins(failing_then_derived: HarnessRun) -> None:
    assert failing_then_derived.report is not None, failing_then_derived.output
    derived = failing_then_derived.report["scenarios"]["derived"]
    for label in ("derived", "hand_written", "prebuilt"):
        assert derived[label]["cpu"] > 0, derived
        assert derived[label]["wall"] > 0, derived
    assert "cpu_overhead_vs_prebuilt_us" in derived


def _own_attributes(cls: ast.ClassDef) -> set[str]:
    """The ``self.<name>`` attributes a harness class assigns itself."""
    return {
        target.attr
        for node in ast.walk(cls)
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign))
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self"
    }


def _private_reads(tree: ast.Module) -> list[str]:
    """Private attributes read off objects the harness does not own, as ``line: expression``."""
    owner: dict[int, set[str]] = {}
    for cls in (node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)):
        own = _own_attributes(cls)
        for node in ast.walk(cls):
            owner.setdefault(id(node), own)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or not node.attr.startswith("_") or node.attr.startswith("__"):
            continue
        on_self = isinstance(node.value, ast.Name) and node.value.id == "self"
        if on_self and node.attr in owner.get(id(node), set()):
            continue
        found.append(f"{node.lineno}: {ast.unparse(node)}")
    return found


@pytest.mark.parametrize("module", ["scenarios.py", "run.py"])
def test_the_harness_uses_no_private_attribute_of_the_framework(module: str) -> None:
    tree = ast.parse((_HARNESS / module).read_text(), filename=module)
    assert _private_reads(tree) == [], (
        f"benchmarks/data/{module} reads private attributes; the redesign may remove them, "
        "and the harness must keep measuring the code before and after it"
    )
