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
"""'pyfly generate' — scaffold individual artifacts into an existing project."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click

from pyfly.cli._project import ProjectInfo, ProjectNotFoundError, detect_project, feature_flags
from pyfly.cli.console import console
from pyfly.cli.naming import Names
from pyfly.cli.naming import names as make_names


@dataclass(frozen=True)
class Artifact:
    """A single file the generator intends to write."""

    kind: str
    path: Path
    content: str


def write_artifacts(
    artifacts: list[Artifact], *, force: bool, dry_run: bool
) -> list[tuple[str, Path]]:
    """Write artifacts, skipping existing files unless ``force``. Returns planned actions."""
    actions: list[tuple[str, Path]] = []
    for art in artifacts:
        exists = art.path.exists()
        if exists and not force:
            actions.append(("skip", art.path))
            continue
        action = "overwrite" if exists else "create"
        actions.append((action, art.path))
        if not dry_run:
            art.path.parent.mkdir(parents=True, exist_ok=True)
            art.path.write_text(art.content)
    return actions


def add_init_export(init_path: Path, statement: str, *, dry_run: bool) -> None:
    """Append an import line to an __init__.py if not already present."""
    existing = init_path.read_text() if init_path.exists() else ""
    if statement in existing:
        return
    if dry_run:
        return
    init_path.parent.mkdir(parents=True, exist_ok=True)
    sep = "" if existing.endswith("\n") or not existing else "\n"
    init_path.write_text(existing + sep + statement + "\n")


def _render(template_name: str, context: dict[str, Any]) -> str:
    from pyfly.cli.templates import _get_env

    return _get_env().get_template(f"generators/{template_name}").render(context)


def _ensure_init(directory: Path, *, dry_run: bool) -> Artifact | None:
    init = directory / "__init__.py"
    if init.exists():
        return None
    return Artifact("init", init, "")


def _context(info: ProjectInfo, raw_name: str) -> dict[str, Any]:
    ctx: dict[str, Any] = {
        "package_name": info.package,
        "archetype": info.archetype,
        "names": make_names(raw_name),
    }
    ctx.update(feature_flags(info))
    return ctx


def _resolve_info(ctx: click.Context) -> ProjectInfo:
    cwd = (ctx.obj or {}).get("cwd") if ctx.obj else None
    try:
        return detect_project(Path(cwd) if cwd else None)
    except ProjectNotFoundError as exc:
        console.print(f"[error]✗[/error] {exc}")
        raise SystemExit(1) from None


def _report(info: ProjectInfo, actions: list[tuple[str, Path]], *, dry_run: bool) -> None:
    from rich.tree import Tree

    label = "Would generate" if dry_run else "Generated"
    tree = Tree(f"[success]{label}[/success]")
    for action, path in actions:
        rel = path.relative_to(info.root)
        color = {"create": "success", "overwrite": "warning", "skip": "dim"}[action]
        tree.add(f"[{color}]{action:9s}[/{color}] {rel}")
    console.print(tree)


def _simple_generate(
    ctx: click.Context,
    name: str,
    *,
    subdir: str,
    suffix: str,
    template: str,
    force: bool,
    dry_run: bool,
) -> None:
    """Generate a single artifact file (+ package __init__) with no extra wiring."""
    info = _resolve_info(ctx)
    context = _context(info, name)
    n: Names = context["names"]
    out_dir = info.package_dir / subdir
    artifacts: list[Artifact] = []
    init = _ensure_init(out_dir, dry_run=dry_run)
    if init:
        artifacts.append(init)
    artifacts.append(Artifact(subdir, out_dir / f"{n.snake}{suffix}.py", _render(template, context)))
    actions = write_artifacts(artifacts, force=force, dry_run=dry_run)
    _report(info, actions, dry_run=dry_run)


def _gen_options(func: Any) -> Any:
    func = click.option("--force", is_flag=True, help="Overwrite existing files.")(func)
    func = click.option("--dry-run", is_flag=True, help="Show what would be created without writing.")(func)
    return func


@click.group(name="generate")
def generate_group() -> None:
    """Generate code artifacts (controller, service, repository, ...)."""


@generate_group.command("service")
@click.argument("name")
@_gen_options
@click.pass_context
def service_cmd(ctx: click.Context, name: str, force: bool, dry_run: bool) -> None:
    """Generate a @service class."""
    info = _resolve_info(ctx)
    context = _context(info, name)
    n: Names = context["names"]
    svc_dir = info.package_dir / "services"
    artifacts: list[Artifact] = []
    init = _ensure_init(svc_dir, dry_run=dry_run)
    if init:
        artifacts.append(init)
    artifacts.append(Artifact("service", svc_dir / f"{n.snake}_service.py", _render("service.py.j2", context)))
    artifacts.append(
        Artifact("test", info.tests_dir / f"test_{n.snake}_service.py", _render("test_service.py.j2", context))
    )
    actions = write_artifacts(artifacts, force=force, dry_run=dry_run)
    _report(info, actions, dry_run=dry_run)


@generate_group.command("controller")
@click.argument("name")
@_gen_options
@click.pass_context
def controller_cmd(ctx: click.Context, name: str, force: bool, dry_run: bool) -> None:
    """Generate a controller (REST or web, archetype-aware)."""
    info = _resolve_info(ctx)
    context = _context(info, name)
    n: Names = context["names"]
    template = "controller_web.py.j2" if info.archetype == "web" else "controller_rest.py.j2"
    ctrl_dir = info.package_dir / "controllers"
    artifacts: list[Artifact] = []
    init = _ensure_init(ctrl_dir, dry_run=dry_run)
    if init:
        artifacts.append(init)
    artifacts.append(Artifact("controller", ctrl_dir / f"{n.snake}_controller.py", _render(template, context)))
    artifacts.append(
        Artifact("test", info.tests_dir / f"test_{n.snake}_controller.py", _render("test_controller.py.j2", context))
    )
    actions = write_artifacts(artifacts, force=force, dry_run=dry_run)
    _report(info, actions, dry_run=dry_run)


@generate_group.command("entity")
@click.argument("name")
@_gen_options
@click.pass_context
def entity_cmd(ctx: click.Context, name: str, force: bool, dry_run: bool) -> None:
    """Generate a model/entity (data-aware)."""
    _simple_generate(ctx, name, subdir="models", suffix="", template="entity.py.j2", force=force, dry_run=dry_run)


@generate_group.command("repository")
@click.argument("name")
@_gen_options
@click.pass_context
def repository_cmd(ctx: click.Context, name: str, force: bool, dry_run: bool) -> None:
    """Generate a repository (data-aware)."""
    _simple_generate(
        ctx,
        name,
        subdir="repositories",
        suffix="_repository",
        template="repository.py.j2",
        force=force,
        dry_run=dry_run,
    )


@generate_group.command("dto")
@click.argument("name")
@_gen_options
@click.pass_context
def dto_cmd(ctx: click.Context, name: str, force: bool, dry_run: bool) -> None:
    """Generate request/response DTOs."""
    _simple_generate(ctx, name, subdir="dto", suffix="_dto", template="dto.py.j2", force=force, dry_run=dry_run)


@generate_group.command("aggregate")
@click.argument("name")
@_gen_options
@click.pass_context
def aggregate_cmd(ctx: click.Context, name: str, force: bool, dry_run: bool) -> None:
    """Generate a DDD aggregate root."""
    _simple_generate(ctx, name, subdir="domain", suffix="", template="aggregate.py.j2", force=force, dry_run=dry_run)


@generate_group.command("command")
@click.argument("name")
@_gen_options
@click.pass_context
def command_cmd(ctx: click.Context, name: str, force: bool, dry_run: bool) -> None:
    """Generate a CQRS command and its handler."""
    info = _resolve_info(ctx)
    context = _context(info, name)
    n: Names = context["names"]
    out = info.package_dir / "cqrs"
    artifacts: list[Artifact] = []
    init = _ensure_init(out, dry_run=dry_run)
    if init:
        artifacts.append(init)
    artifacts.append(Artifact("command", out / f"{n.snake}_command.py", _render("command.py.j2", context)))
    artifacts.append(Artifact("handler", out / f"{n.snake}_handler.py", _render("command_handler.py.j2", context)))
    actions = write_artifacts(artifacts, force=force, dry_run=dry_run)
    _report(info, actions, dry_run=dry_run)


@generate_group.command("query")
@click.argument("name")
@_gen_options
@click.pass_context
def query_cmd(ctx: click.Context, name: str, force: bool, dry_run: bool) -> None:
    """Generate a CQRS query and its handler."""
    info = _resolve_info(ctx)
    context = _context(info, name)
    n: Names = context["names"]
    out = info.package_dir / "cqrs"
    artifacts: list[Artifact] = []
    init = _ensure_init(out, dry_run=dry_run)
    if init:
        artifacts.append(init)
    artifacts.append(Artifact("query", out / f"{n.snake}_query.py", _render("query.py.j2", context)))
    artifacts.append(Artifact("handler", out / f"{n.snake}_handler.py", _render("query_handler.py.j2", context)))
    actions = write_artifacts(artifacts, force=force, dry_run=dry_run)
    _report(info, actions, dry_run=dry_run)


@generate_group.command("event")
@click.argument("name")
@_gen_options
@click.pass_context
def event_cmd(ctx: click.Context, name: str, force: bool, dry_run: bool) -> None:
    """Generate a domain event and a sample listener."""
    info = _resolve_info(ctx)
    context = _context(info, name)
    n: Names = context["names"]
    out = info.package_dir / "events"
    artifacts: list[Artifact] = []
    init = _ensure_init(out, dry_run=dry_run)
    if init:
        artifacts.append(init)
    artifacts.append(Artifact("event", out / f"{n.snake}_event.py", _render("event.py.j2", context)))
    artifacts.append(Artifact("listener", out / f"{n.snake}_listener.py", _render("event_listener.py.j2", context)))
    actions = write_artifacts(artifacts, force=force, dry_run=dry_run)
    _report(info, actions, dry_run=dry_run)


@generate_group.command("saga")
@click.argument("name")
@_gen_options
@click.pass_context
def saga_cmd(ctx: click.Context, name: str, force: bool, dry_run: bool) -> None:
    """Generate a saga orchestration skeleton."""
    _simple_generate(ctx, name, subdir="sagas", suffix="_saga", template="saga.py.j2", force=force, dry_run=dry_run)
