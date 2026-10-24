"""Native Agentic selection through the CLI and generated application lifecycle."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner
from packaging.requirements import Requirement

from pyfly.cli.main import cli


def test_catalog_exposes_agentic() -> None:
    result = CliRunner().invoke(cli, ["new", "--list"])
    assert result.exit_code == 0, result.output
    assert "agentic" in result.output


@pytest.mark.parametrize("interactive", [False, True])
def test_agentic_selection_generates_explicit_native_config(tmp_path: Path, interactive: bool) -> None:
    args = ["new", "--directory", str(tmp_path)]
    with patch("pyfly.cli.new.questionary") as prompts:
        if interactive:
            prompts.text.return_value.unsafe_ask.side_effect = ["agent-service", "agent_service"]
            prompts.select.return_value.unsafe_ask.return_value = "core"
            prompts.checkbox.return_value.unsafe_ask.return_value = ["agentic"]
            prompts.confirm.return_value.unsafe_ask.return_value = True
        else:
            args.extend(["agent-service", "--archetype", "core", "--features", "agentic", "--no-input"])
        result = CliRunner().invoke(cli, args)
        if interactive:
            choices = prompts.checkbox.call_args.kwargs["choices"]
            assert any(getattr(choice, "value", None) == "agentic" for choice in choices)

    assert result.exit_code == 0, result.output
    project = tmp_path / "agent-service"
    config = yaml.safe_load((project / "pyfly.yaml").read_text())["pyfly"]
    assert config["agentic"] == {"enabled": True}
    assert "data" not in config
    dependencies = tomllib.loads((project / "pyproject.toml").read_text())["project"]["dependencies"]
    assert dependencies == [
        "pyfly[agentic] @ https://github.com/fireflyframework/fireflyframework-pyfly/"
        "releases/download/v26.10.09/pyfly-26.10.9-py3-none-any.whl"
    ]


@pytest.mark.parametrize(
    ("features", "extras"),
    [
        ("agentic", {"agentic"}),
        ("web,agentic", {"web", "agentic"}),
        ("fastapi,agentic", {"fastapi", "agentic"}),
        ("web,fastapi,agentic", {"web-fastapi", "agentic"}),
        ("cache,data-relational,agentic", {"cache", "data-relational", "agentic"}),
    ],
)
@pytest.mark.parametrize(
    ("version", "tag", "wheel_version"),
    [("26.10.09", "v26.10.09", "26.10.9"), ("26.1.02", "v26.01.02", "26.1.2")],
)
def test_agentic_scaffold_pins_official_wheel_for_cli_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    features: str,
    extras: set[str],
    version: str,
    tag: str,
    wheel_version: str,
) -> None:
    monkeypatch.setattr("pyfly.__version__", version)
    runner = CliRunner()
    result = runner.invoke(cli, ["new", "pinned-agent", "--features", features, "--directory", str(tmp_path)])
    assert result.exit_code == 0, result.output
    project = tmp_path / "pinned-agent"
    manifest = project / "pyproject.toml"
    dependency = tomllib.loads(manifest.read_text())["project"]["dependencies"][0]
    requirement = Requirement(dependency)
    assert requirement.name == "pyfly"
    assert requirement.extras == extras
    expected_url = (
        "https://github.com/fireflyframework/fireflyframework-pyfly/releases/"
        f"download/{tag}/pyfly-{wheel_version}-py3-none-any.whl"
    )
    assert requirement.url == expected_url
    for command in (["remove", "agentic", "--yes"], ["add", "agentic"]):
        updated = runner.invoke(cli, command, obj={"cwd": project})
        assert updated.exit_code == 0, updated.output
        requirement = Requirement(tomllib.loads(manifest.read_text())["project"]["dependencies"][0])
        assert requirement.url == expected_url
    assert requirement.extras == extras


def test_default_core_does_not_enable_agentic(tmp_path: Path) -> None:
    result = CliRunner().invoke(cli, ["new", "plain-service", "--directory", str(tmp_path), "--no-input"])
    assert result.exit_code == 0, result.output
    project = tmp_path / "plain-service"
    assert "agentic" not in yaml.safe_load((project / "pyfly.yaml").read_text())["pyfly"]
    assert tomllib.loads((project / "pyproject.toml").read_text())["project"]["dependencies"] == ["pyfly"]


@pytest.mark.parametrize("add_to_existing", [False, True])
def test_generated_agentic_project_builds_with_direct_wheel_dependency(tmp_path: Path, add_to_existing: bool) -> None:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is required for the isolated generated-project build")
    runner = CliRunner()
    args = ["new", "build-agent", "--directory", str(tmp_path)]
    if not add_to_existing:
        args.extend(["--features", "agentic"])
    generated = runner.invoke(cli, args)
    assert generated.exit_code == 0, generated.output
    project = tmp_path / "build-agent"
    if add_to_existing:
        added = runner.invoke(cli, ["add", "agentic"], obj={"cwd": project})
        assert added.exit_code == 0, added.output
    dependency = tomllib.loads((project / "pyproject.toml").read_text())["project"]["dependencies"][0]
    build = subprocess.run(
        [uv, "build", "--offline", "--python", sys.executable, "--wheel", "--out-dir", str(tmp_path / "dist")],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    (wheel,) = (tmp_path / "dist").glob("*.whl")
    with zipfile.ZipFile(wheel) as archive:
        (metadata,) = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        requires = BytesParser().parsebytes(archive.read(metadata)).get_all("Requires-Dist")
    assert requires is not None
    (requirement,) = [Requirement(item) for item in requires]
    assert requirement.name == "pyfly"
    assert requirement.extras == {"agentic"}
    assert requirement.url == Requirement(dependency).url


def test_new_plain_core_then_add_agentic_pins_native_release(tmp_path: Path) -> None:
    runner = CliRunner()
    created = runner.invoke(cli, ["new", "existing-agent", "--directory", str(tmp_path)])
    assert created.exit_code == 0, created.output
    project = tmp_path / "existing-agent"
    added = runner.invoke(cli, ["add", "agentic"], obj={"cwd": project})
    assert added.exit_code == 0, added.output
    manifest = tomllib.loads((project / "pyproject.toml").read_text())
    (requirement,) = [Requirement(item) for item in manifest["project"]["dependencies"]]
    assert requirement.extras == {"agentic"}
    assert requirement.url == (
        "https://github.com/fireflyframework/fireflyframework-pyfly/releases/"
        "download/v26.10.09/pyfly-26.10.9-py3-none-any.whl"
    )


@pytest.mark.parametrize("rest", ["", "; python_version >= '3.12'", "  ; python_version >= '3.12'"])
def test_add_agentic_to_unversioned_dependency_preserves_extras_and_literal_marker(tmp_path: Path, rest: str) -> None:
    manifest = tmp_path / "pyproject.toml"
    manifest.write_text('[project]\nname = "app"\ndependencies = ["pyfly[web]' + rest + '"]\n')
    added = CliRunner().invoke(cli, ["add", "agentic"], obj={"cwd": tmp_path})
    assert added.exit_code == 0, added.output
    (dependency,) = tomllib.loads(manifest.read_text())["project"]["dependencies"]
    tail = " " + rest if rest.startswith(";") else rest
    assert dependency == (
        "pyfly[agentic,web] @ https://github.com/fireflyframework/fireflyframework-pyfly/"
        "releases/download/v26.10.09/pyfly-26.10.9-py3-none-any.whl" + tail
    )
    assert Requirement(dependency).extras == {"agentic", "web"}


@pytest.mark.parametrize(
    "constraint", [">=26.0", "==26.9.1", "~=26.10", " (>=26.0)", "(>=26.0)", ">=26.0 ; python_version >= '3.12'"]
)
def test_add_agentic_rejects_version_constraint_without_mutation(tmp_path: Path, constraint: str) -> None:
    manifest = tmp_path / "pyproject.toml"
    original = '[project]\nname = "app"\ndependencies = ["pyfly[web]' + constraint + '"]\n'
    manifest.write_text(original)
    added = CliRunner().invoke(cli, ["add", "agentic"], obj={"cwd": tmp_path})
    assert added.exit_code == 1, added.output
    assert "version constraint" in added.output
    assert "official release wheel" in added.output
    assert "v26.10.09/pyfly-26.10.9-py3-none-any.whl" in added.output
    assert manifest.read_text() == original


@pytest.mark.parametrize("separator", [" @ ", "@"])
def test_add_list_remove_agentic_preserves_direct_wheel_reference(tmp_path: Path, separator: str) -> None:
    reference = (
        separator + "https://github.com/fireflyframework/fireflyframework-pyfly/releases/"
        "download/v26.10.09/pyfly-26.10.9-py3-none-any.whl ; python_version >= '3.12'"
    )
    manifest = tmp_path / "pyproject.toml"
    manifest.write_text('[project]\nname = "app"\ndependencies = ["pyfly[web]' + reference + '"]\n')
    runner = CliRunner()
    added = runner.invoke(cli, ["add", "agentic"], obj={"cwd": tmp_path})
    assert added.exit_code == 0, added.output
    assert tomllib.loads(manifest.read_text())["project"]["dependencies"] == ["pyfly[agentic,web]" + reference]
    listing = runner.invoke(cli, ["features"], obj={"cwd": tmp_path})
    assert listing.exit_code == 0, listing.output
    assert any("✓" in line and "agentic" in line for line in listing.output.splitlines())
    removed = runner.invoke(cli, ["remove", "agentic", "--yes"], obj={"cwd": tmp_path})
    assert removed.exit_code == 0, removed.output
    assert tomllib.loads(manifest.read_text())["project"]["dependencies"] == ["pyfly[web]" + reference]


def test_add_agentic_keeps_one_existing_source_for_multiple_pyfly_requirements(tmp_path: Path) -> None:
    reference = " @ https://example.invalid/releases/pyfly-26.9.3-py3-none-any.whl"
    manifest = tmp_path / "pyproject.toml"
    manifest.write_text(
        '[project]\nname = "app"\ndependencies = ["pyfly[web]' + reference + '", "pyfly[templates]", "pyfly>=26.9"]\n'
    )
    added = CliRunner().invoke(cli, ["add", "agentic"], obj={"cwd": tmp_path})
    assert added.exit_code == 0, added.output
    assert tomllib.loads(manifest.read_text())["project"]["dependencies"] == [
        "pyfly[agentic,web]" + reference,
        "pyfly[agentic,templates]",
        "pyfly[agentic]>=26.9",
    ]


@pytest.mark.parametrize("metadata", ["allow-direct-references = false", "allow-ambiguous-features = true"])
def test_add_agentic_preserves_existing_hatch_metadata_policy(tmp_path: Path, metadata: str) -> None:
    runner = CliRunner()
    created = runner.invoke(cli, ["new", "hatch-agent", "--directory", str(tmp_path)])
    assert created.exit_code == 0, created.output
    project = tmp_path / "hatch-agent"
    manifest = project / "pyproject.toml"
    original = manifest.read_text() + "\n[tool.hatch.metadata]\n" + metadata + "\n"
    manifest.write_text(original)
    added = runner.invoke(cli, ["add", "agentic"], obj={"cwd": project})
    if metadata == "allow-direct-references = false":
        assert added.exit_code == 1, added.output
        assert "allow-direct-references=true" in added.output
        assert manifest.read_text() == original
    else:
        assert added.exit_code == 0, added.output
        config = tomllib.loads(manifest.read_text())["tool"]["hatch"]["metadata"]
        assert config == {"allow-ambiguous-features": True, "allow-direct-references": True}


@pytest.mark.parametrize("inline", ["[tool.hatch]\nmetadata = {}", "[tool]\nhatch = {}"])
def test_add_agentic_rejects_unsupported_inline_hatch_without_mutation(tmp_path: Path, inline: str) -> None:
    manifest = tmp_path / "pyproject.toml"
    original = (
        '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n'
        '[project]\nname = "app"\ndependencies = ["pyfly"]\n' + inline + "\n"
    )
    manifest.write_text(original)
    assert tomllib.loads(original)["project"]["dependencies"] == ["pyfly"]
    added = CliRunner().invoke(cli, ["add", "agentic"], obj={"cwd": tmp_path})
    assert added.exit_code == 1, added.output
    assert "allow-direct-references=true" in added.output
    assert manifest.read_text() == original


def test_generated_agentic_application_injects_factory_and_shuts_down(tmp_path: Path) -> None:
    pytest.importorskip("pydantic_ai")
    result = CliRunner().invoke(
        cli, ["new", "native-agent", "--features", "agentic", "--directory", str(tmp_path), "--no-input"]
    )
    assert result.exit_code == 0, result.output
    project = tmp_path / "native-agent"
    (project / "src/native_agent/worker.py").write_text(
        "from pyfly.container import service\n"
        "from pyfly.agentic.factory import AgentFactory\n\n"
        "@service\n"
        "class Worker:\n"
        "    def __init__(self, factory: AgentFactory) -> None:\n"
        "        self.factory = factory\n"
    )
    code = """
import asyncio
from pydantic_ai import models
from pydantic_ai.models.test import TestModel
from pyfly.agentic.factory import AgentFactory
from pyfly.agentic.runtime import AgenticRuntime
from pyfly.core import PyFlyApplication
from native_agent.app import Application
from native_agent.worker import Worker

models.ALLOW_MODEL_REQUESTS = False

async def main():
    app = PyFlyApplication(Application)
    await app.startup()
    try:
        runtime = app.context.get_bean(AgenticRuntime)
        factory = app.context.get_bean(Worker).factory
        assert factory is app.context.get_bean(AgentFactory)
        agent = factory.create('scaffold-proof', model=TestModel())
        assert runtime.agent_registry.get('scaffold-proof') is agent
        assert (await agent.run('hello')).output
    finally:
        await app.shutdown()
    assert runtime._closed
    print('native-agentic-scaffold-ok')

asyncio.run(main())
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(project / "src"), str(Path(__file__).resolve().parents[2] / "src")])
    proof = subprocess.run(
        [sys.executable, "-c", code], cwd=project, env=env, capture_output=True, text=True, timeout=60, check=False
    )
    assert proof.returncode == 0, proof.stdout + proof.stderr
    assert "native-agentic-scaffold-ok" in proof.stdout
