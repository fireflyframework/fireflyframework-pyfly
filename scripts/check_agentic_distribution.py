"""Qualify wheel/sdist contents and isolated base/Agentic installations.

Requires the uv command. Run once per supported interpreter; every invocation
creates new environments outside the checkout and retains all command logs.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

INSTALLED_SMOKE = r'''"""Executed with python -I from outside both source checkouts."""

import asyncio
import hashlib
import importlib
import importlib.metadata
import importlib.resources
import importlib.util
import json
import logging
import sys
import threading
from pathlib import Path

import pyfly

MODE = sys.argv[1]
CHECKOUT = Path(sys.argv[2]).resolve()
assert all(not Path(path or ".").resolve().is_relative_to(CHECKOUT) for path in sys.path)
assert importlib.util.find_spec("fireflyframework_agentic") is None
for distribution in importlib.metadata.distributions():
    assert distribution.metadata["Name"].lower().replace("_", "-") != "fireflyframework-agentic"


async def base():
    before_threads = {id(t) for t in threading.enumerate()}
    before_handlers = list(logging.getLogger().handlers)
    for package in (
        "pydantic_ai", "pydantic_graph", "openai", "anthropic", "genai_prices", "opentelemetry",
        "sqlalchemy", "aiosqlite", "asyncpg", "beanie", "pymongo", "motor", "pydantic_monty",
    ):
        assert importlib.util.find_spec(package) is None, package
    from pyfly.context import ApplicationContext
    from pyfly.core.config import Config
    import pyfly.agentic

    entry = [
        ep for ep in importlib.metadata.entry_points(group="pyfly.auto_configuration") if ep.name == "agentic"
    ]
    assert len(entry) == 1
    entry[0].load()
    ctx = ApplicationContext(Config({"pyfly": {"agentic": {"enabled": False}}}))
    await ctx.start()
    await ctx.stop()
    for package in (
        "pyfly.agentic.config",
        "pyfly.agentic.runtime",
        "pydantic_ai",
        "pydantic_graph",
        "openai",
        "anthropic",
        "genai_prices",
        "opentelemetry",
        "sqlalchemy",
        "beanie",
        "pymongo",
    ):
        assert package not in sys.modules, package
    assert {id(t) for t in threading.enumerate()} == before_threads
    assert list(logging.getLogger().handlers) == before_handlers
    enabled = ApplicationContext(Config({"pyfly": {"agentic": {"enabled": True}}}))
    try:
        await enabled.start()
    except Exception as exc:
        assert "pyfly[agentic]" in str(exc), str(exc)
        assert "missing:" in str(exc), str(exc)
    else:
        raise AssertionError("Enabling missing AI dependencies unexpectedly succeeded")
    finally:
        await enabled.stop()
    return {
        "disabled_context": "passed",
        "no_AI_distributions_imports_threads_handlers": "passed",
        "enabled_missing_extra_error": "passed",
    }


async def agentic():
    from pydantic_ai.models.test import TestModel
    import pydantic_ai.models
    from pyfly.agentic import Agent, AgentTool, AgenticConfig, agent, agent_tool
    from pyfly.agentic.agents.registry import AgentRegistry
    from pyfly.agentic.factory import AgentFactory
    from pyfly.agentic.runtime import AgenticRuntime
    from pyfly.agentic.observability import AgentTracer, AgentMetrics, AgentEvent, AgentEvents
    from pyfly.agentic.workflows import ManagedAgentRunner
    from pyfly.container import service
    from pyfly.context import ApplicationContext
    from pyfly.core.config import Config

    pydantic_ai.models.ALLOW_MODEL_REQUESTS = False
    names = [
        Agent,
        AgentTool,
        AgenticConfig,
        ManagedAgentRunner,
        AgentTracer,
        AgentMetrics,
        AgentEvent,
        AgentEvents,
        agent,
        agent_tool,
    ]
    assert len(names) == 10
    manifest = json.loads((CHECKOUT / "scripts/agentic-migration-manifest.json").read_text())
    exported = 0
    for item in manifest["modules"]:
        if not item["exports"]:
            continue
        module_name = (
            item["target"]
            .removeprefix("src/")
            .removesuffix(".py")
            .replace("/", ".")
            .removesuffix(".__init__")
        )
        module = importlib.import_module(module_name)
        assert Path(module.__file__).resolve().is_relative_to(Path(sys.prefix)), module.__file__
        for name in item["exports"]:
            assert hasattr(module, name), (module_name, name)
            exported += 1
    for module in list(sys.modules.values()):
        namespace = getattr(module, "__name__", "")
        if namespace.startswith("pyfly.agentic"):
            for old_name in manifest["renames"]:
                assert old_name not in vars(module), (namespace, old_name)
    assert importlib.resources.files("pyfly.agentic").joinpath("py.typed").is_file()

    @service
    class SupportService:
        def __init__(self, agents: AgentFactory):
            self.agent = agents.create(
                "lumen-installed-service", model=TestModel(custom_output_text="Lumen support is ready.")
            )

        async def answer(self):
            return (await self.agent.run("Introduce support.")).output

    ctx = ApplicationContext(
        Config(
            {
                "pyfly": {
                    "agentic": {
                        "enabled": True,
                        "default-model": "test",
                        "plugin-auto-discover": False,
                        "observability-enabled": False,
                    }
                }
            }
        )
    )
    ctx.register_bean(SupportService)
    await ctx.start()
    runtime = ctx.get_bean(AgenticRuntime)
    try:
        service_instance = ctx.get_bean(SupportService)
        assert await service_instance.answer() == "Lumen support is ready."
        assert ctx.get_bean(AgentRegistry).get("lumen-installed-service") is service_instance.agent
        assert ctx.get_bean(AgentFactory) is runtime.agent_factory
        assert len(runtime.usage_tracker.records) == 1
    finally:
        await ctx.stop()
    assert runtime._closed
    assert not runtime._tasks and not runtime._streams
    persistence = await relational()
    execution = await script_execution()
    return {
        "native_DI_TestModel_service": "passed",
        "public_export_resolutions": exported,
        "no_legacy_runtime_aliases": "passed",
        "typed_resource": "passed",
        "runtime_shutdown": "passed",
        "provider_requests": "disabled",
        **persistence,
        **execution,
    }


async def script_execution():
    from pyfly.agentic import agent_tool
    from pyfly.agentic.execution import MontyEnvironment, toolkit_external_functions, ExecutionLimits

    @agent_tool("installed_double", auto_register=False)
    async def installed_double(value: int) -> int:
        """Double a fictional test value."""
        return value * 2

    environment = MontyEnvironment()
    functions = toolkit_external_functions([installed_double])
    result = await environment.run_code("print('installed worker')\ninstalled_double(n)", inputs={"n": 21},
                                        external_functions=functions)
    assert result.success and result.output == 42 and result.stdout == "installed worker\n"
    try:
        await environment.run_code("1 + 1", limits=ExecutionLimits(max_allocations=100))
    except ValueError as exc:
        assert "not supported" in str(exc)
    else:
        raise AssertionError("Unsupported allocation limit was accepted")
    return {"native_Monty_worker_and_Code_Mode": "passed", "unsupported_allocation_limit_rejected": True}


async def relational():
    from sqlalchemy import select
    from pyfly.agentic.memory.manager import MemoryManager
    from pyfly.context import ApplicationContext
    from pyfly.core.config import Config
    from pyfly.data.relational.datasource_registry import DataSourceRegistry
    from pyfly.data.relational.framework_schema import agentic_memory_table
    from pyfly.data.transaction import TransactionTemplate

    assert importlib.util.find_spec("motor") is None
    configuration = {
        "pyfly": {
            "data": {"relational": {
                "enabled": True,
                "url": f"sqlite+aiosqlite:///{Path(sys.prefix) / 'installed.db'}",
                "ddl-auto": "create",
            }},
            "agentic": {
                "enabled": True, "memory_backend": "relational", "encryption_enabled": True,
                "encryption_key": "fixture-key-with-exactly-32-bytes!",
                "plugin_auto_discover": False, "observability_enabled": False,
            },
        },
    }
    context = ApplicationContext(Config(configuration))
    await context.start()
    try:
        memory = context.get_bean(MemoryManager)
        source = context.get_bean(DataSourceRegistry).primary
        try:
            async with TransactionTemplate(source).transaction():
                await memory.async_set_fact("rejected", {"amount": 12})
                assert await memory.async_get_fact("rejected") == {"amount": 12}
                raise RuntimeError("rollback fixture")
        except RuntimeError as error:
            assert str(error) == "rollback fixture"
        else:
            raise AssertionError("Rollback fixture unexpectedly committed")
        assert await memory.async_get_fact("rejected") is None
        await memory.async_set_fact("accepted", {"amount": [12, False, None]})
        table = agentic_memory_table()
        async with TransactionTemplate(source).transaction(read_only=True) as unit:
            row = (await unit.resource.execute(select(table.c.content).where(table.c.key == "accepted"))).scalar_one()
        assert isinstance(row["content"], str) and "amount" not in row["content"]
    finally:
        await context.stop()
    restored_context = ApplicationContext(Config(configuration))
    await restored_context.start()
    try:
        restored = restored_context.get_bean(MemoryManager)
        assert restored is not memory
        assert await restored.async_get_fact("accepted") == {"amount": [12, False, None]}
        assert await restored.async_get_fact("rejected") is None
    finally:
        await restored_context.stop()
    return {
        "native_Data_SQLite_encrypted_persistence": "passed",
        "native_Data_transaction_rollback": "passed",
        "native_Data_cold_context_restart": "passed",
    }


assets = importlib.resources.files("pyfly.admin").joinpath("static/assets")
provenance = json.loads(assets.joinpath("brand-origin.json").read_text())
for name, metadata in provenance["files"].items():
    assert hashlib.sha256(assets.joinpath(name).read_bytes()).hexdigest() == metadata["sha256"], name
result = asyncio.run(base() if MODE == "base" else agentic())
result.update(
    {
        "canonical_admin_assets_verified": len(provenance["files"]),
        "mode": MODE,
        "python": sys.version.split()[0],
        "pyfly_version": importlib.metadata.version("pyfly"),
        "pyfly_display_version": pyfly.__version__,
        "installed_prefix": sys.prefix,
        "working_directory": str(Path.cwd()),
    }
)
print("RESULT_JSON=" + json.dumps(result, sort_keys=True))
'''


def normalized_requirement(value: str) -> str:
    match = re.fullmatch(r"([A-Za-z0-9_.-]+)(?:\[([^]]+)\])?(.*)", value.strip())
    if match is None:
        raise ValueError(f"Invalid package requirement: {value}")
    name, extras, constraints = match.groups()
    name = re.sub(r"[-_.]+", "-", name.lower())
    names = sorted(extras.lower().split(",")) if extras else []
    bounds = sorted(constraints.replace(" ", "").split(","))
    return name + ("[" + ",".join(names) + "]" if names else "") + ",".join(bounds)


def check_dependency_metadata(content: dict[str, bytes], manifest: dict[str, Any]) -> int:
    if "extras" not in manifest:
        return 0
    metadata = [data for name, data in content.items() if name.endswith(".dist-info/METADATA") or name == "PKG-INFO"]
    if len(metadata) != 1:
        raise ValueError("Distribution must contain one package metadata document")
    document = BytesParser().parsebytes(metadata[0])
    requirements = [str(value) for value in document.get_all("Requires-Dist", [])]
    provided = {str(value) for value in document.get_all("Provides-Extra", [])}

    def expanded(extra: str, trail: frozenset[str] = frozenset()) -> set[str]:
        if extra in trail:
            raise ValueError(f"Cyclic native extra dependency: {extra}")
        dependencies: set[str] = set()
        contracts = manifest.get("supporting_extras", {}) | manifest["extras"]
        for value in contracts[extra]:
            self_reference = re.fullmatch(r"pyfly\[([^]]+)\]", value)
            if self_reference:
                for selected in self_reference[1].split(","):
                    dependencies.update(expanded(selected, trail | {extra}))
            else:
                dependencies.add(normalized_requirement(value))
        return dependencies

    for extra in manifest["extras"]:
        actual = set()
        for requirement in requirements:
            head, _, marker = requirement.partition(";")
            match = re.search(r"extra\s*==\s*['\"]([^'\"]+)['\"]", marker)
            if match and match[1] == extra:
                actual.add(normalized_requirement(head))
        if extra not in provided or actual != expanded(extra):
            raise ValueError(f"Optional dependency metadata differs from the reviewed manifest: {extra}")
    for requirement in requirements:
        if "fireflyframework-agentic" in requirement.lower():
            raise ValueError("Standalone Agentic distribution dependency remains in package metadata")
        if ";" not in requirement and any(name in requirement.lower() for name in ("pydantic-ai", "pydantic-graph")):
            raise ValueError(f"AI dependency leaked into base package metadata: {requirement}")
    return len(manifest["extras"])


def inspect_archive(artifact: Path, root: Path, manifest: dict[str, Any]) -> dict[str, str | int]:
    """Reject missing/stale native files and accidentally packaged environments."""
    content: dict[str, bytes] = {}
    if artifact.suffix == ".whl":
        with zipfile.ZipFile(artifact) as archive:
            content = {name: archive.read(name) for name in archive.namelist() if not name.endswith("/")}
    else:
        with tarfile.open(artifact) as archive:
            for member in archive.getmembers():
                if member.isfile():
                    stream = archive.extractfile(member)
                    assert stream is not None
                    content[member.name.split("/", 1)[1]] = stream.read()
    excluded = [
        name
        for name in content
        if any(
            part.startswith(".venv") or part in {"venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
            for part in Path(name).parts
        )
        or name.startswith(("book/dist/", "book/release-output/", "book/release-qa/"))
    ]
    if excluded:
        raise ValueError(f"Generated environment/cache/QA files in {artifact.name}: {excluded}")
    expected = {
        path.relative_to(root).as_posix()
        for path in (root / "src/pyfly").rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    if artifact.suffix == ".whl":
        expected = {name.removeprefix("src/") for name in expected}
    else:
        expected.update(item["target"] for item in manifest["files"])
        expected.update(
            {
                "scripts/verify_agentic_migration.py",
                "scripts/check_agentic_distribution.py",
                "scripts/agentic-migration-manifest.json",
                "book/build/pdf.py",
                "book/build/epub.py",
                "book/build/brand_diagrams.py",
                "book/book.yaml",
                "book/book.es.yaml",
            }
        )
        expected.update(path.relative_to(root).as_posix() for path in (root / "book/art").rglob("*.svg"))
    if missing := sorted(expected - content.keys()):
        raise ValueError(f"Missing required archive files in {artifact.name}: {missing}")
    prefix = "pyfly/agentic/" if artifact.suffix == ".whl" else "src/pyfly/agentic/"
    native = {name: data for name, data in content.items() if name.startswith(prefix)}
    package_prefix = "pyfly/" if artifact.suffix == ".whl" else "src/pyfly/"
    package = {name: data for name, data in content.items() if name.startswith(package_prefix)}
    for name, data in package.items():
        source = root / ("src/" + name if artifact.suffix == ".whl" else name)
        if not source.is_file() or source.read_bytes() != data:
            raise ValueError(f"Archived runtime differs from native source: {name}")
    metadata_contracts = check_dependency_metadata(content, manifest)
    return {
        "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "bytes": artifact.stat().st_size,
        "archive_files": len(content),
        "agentic_runtime_files": len(native),
        "native_package_files_verified": len(package),
        "agentic_python_modules": sum(name.endswith(".py") for name in native),
        "required_files_verified": len(expected),
        "excluded_environment_cache_generated_files": len(excluded),
        "optional_extra_metadata_contracts_verified": metadata_contracts,
    }


def installed_lane(artifact: Path, mode: str, python: str, work: Path, root: Path) -> dict[str, Any]:
    kind = "wheel" if artifact.suffix == ".whl" else "sdist"
    label = f"{kind}-{mode}"
    environment = work / label
    executable = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    commands = [
        ["uv", "venv", "--python", python, str(environment)],
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(executable),
            str(artifact)
            + ("[agentic-relational,agentic-security,agentic-script-execution]" if mode == "agentic" else ""),
        ],
        [str(executable), "-I", "-c", INSTALLED_SMOKE, mode, str(root)],
    ]
    logs = []
    for index, command in enumerate(commands):
        log = work / f"{label}-{index}.log"
        logs.append(str(log))
        with log.open("w", encoding="utf-8") as stream:
            result = subprocess.run(
                command,
                cwd=work,
                stdout=stream,
                stderr=subprocess.STDOUT,
                env=os.environ | {"PYDANTIC_AI_NO_BANNER": "1"},
                timeout=600,
                check=False,
            )
        if result.returncode:
            return {"lane": label, "passed": False, "exit_code": result.returncode, "logs": logs}
    output = Path(logs[-1]).read_text(encoding="utf-8").splitlines()
    smoke: dict[str, Any] = json.loads(
        next(line.removeprefix("RESULT_JSON=") for line in output if line.startswith("RESULT_JSON="))
    )
    expected_version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    version_source = ast.parse((root / "src/pyfly/__init__.py").read_text(encoding="utf-8"))
    display_versions = [
        node.value.value
        for node in version_source.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    ]
    if len(display_versions) != 1:
        raise ValueError("Native source must declare one literal display version")
    if smoke.get("pyfly_version") != expected_version or smoke.get("pyfly_display_version") != display_versions[0]:
        return {"lane": label, "passed": False, "error": "Installed version differs from native project", "logs": logs}
    return smoke | {"lane": label, "passed": True, "logs": logs}


def select_artifacts(dist_dir: Path | None, artifacts: list[Path] | None) -> list[Path]:
    if dist_dir is not None:
        wheels = sorted(dist_dir.glob("*.whl"))
        sdists = sorted(dist_dir.glob("*.tar.gz"))
        if len(wheels) != 1 or len(sdists) != 1:
            raise ValueError("--dist-dir must contain exactly one wheel and one sdist")
        return [*wheels, *sdists]
    if not artifacts:
        raise ValueError("No distribution artifacts selected")
    return artifacts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--dist-dir", type=Path, help="Directory containing one candidate wheel and one sdist")
    source.add_argument("--artifact", action="append", type=Path, help="An individual wheel or sdist (repeatable)")
    parser.add_argument("--python", default=sys.executable, help="Interpreter used to create fresh environments")
    parser.add_argument("--work-dir", type=Path, required=True, help="Log/environment parent outside the checkout")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--json", type=Path, help="Result file (defaults to the generated run directory)")
    args = parser.parse_args()
    if Path(args.python).is_file():
        args.python = str(Path(args.python).resolve())
    root = args.root.resolve()
    parent = args.work_dir.resolve()
    if parent.is_relative_to(root):
        parser.error("--work-dir must be outside the source checkout")
    parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="agentic-distribution-", dir=parent))
    try:
        artifacts = select_artifacts(args.dist_dir, args.artifact)
    except ValueError as exc:
        parser.error(str(exc))
    manifest = json.loads((root / "scripts/agentic-migration-manifest.json").read_text(encoding="utf-8"))
    archives = {path.name: inspect_archive(path.resolve(), root, manifest) for path in artifacts}
    lanes = [
        installed_lane(path.resolve(), mode, args.python, work, root)
        for path in artifacts
        for mode in ("base", "agentic")
    ]
    report = {
        "scope": "complete-wheel-and-sdist" if args.dist_dir else "selected-artifacts-only",
        "source_commit": manifest["source"]["commit"],
        "target_release_commit": None,
        "candidate_version": tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"],
        "archives": archives,
        "matrix": lanes,
        "work_directory": str(work),
        "passed": all(lane["passed"] for lane in lanes),
    }
    report_path = args.json or work / "report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    raise SystemExit(not report["passed"])


if __name__ == "__main__":
    main()
