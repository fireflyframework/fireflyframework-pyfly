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
"""'pyfly routes/beans/env/health/metrics/conditions/actuator' — app introspection."""

from __future__ import annotations

import json
from typing import Any

import click

from pyfly.cli._introspect import ActuatorClient, boot_context, run_async
from pyfly.cli.console import console, err_console


def _emit(data: Any, *, as_json: bool, title: str) -> None:
    if as_json:
        # Emit raw JSON via click.echo (NOT rich) so output is exact and pipeable —
        # rich's print_json wraps long lines at the console width, corrupting JSON.
        click.echo(json.dumps(data, indent=2))
        return
    from rich.pretty import Pretty

    console.print(f"[bold]{title}[/bold]")
    console.print(Pretty(data))


def _url_option(func: Any) -> Any:
    func = click.option("--url", default=None, help="Introspect a running app via its /actuator endpoints.")(func)
    func = click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")(func)
    return func


@click.command("routes")
@_url_option
def routes_cmd(url: str | None, as_json: bool) -> None:
    """List HTTP route mappings."""
    if url:
        data = ActuatorClient(url).get("mappings")
    else:
        from pyfly.actuator.endpoints.mappings_endpoint import MappingsEndpoint

        ctx = boot_context()
        data = run_async(MappingsEndpoint(ctx).handle())
    _emit(data, as_json=as_json, title="Routes")


@click.command("beans")
@_url_option
def beans_cmd(url: str | None, as_json: bool) -> None:
    """List container beans."""
    if url:
        data = ActuatorClient(url).get("beans")
    else:
        from pyfly.admin.providers.beans_provider import BeansProvider

        ctx = boot_context()
        data = run_async(BeansProvider(ctx).get_beans())
    _emit(data, as_json=as_json, title="Beans")


@click.command("conditions")
@_url_option
def conditions_cmd(url: str | None, as_json: bool) -> None:
    """Show the auto-configuration condition report."""
    if url:
        data = ActuatorClient(url).get("conditions")
    else:
        from pyfly.actuator.endpoints.conditions_endpoint import ConditionsEndpoint

        ctx = boot_context()
        data = run_async(ConditionsEndpoint(ctx).handle())
    _emit(data, as_json=as_json, title="Conditions")


@click.command("env")
@_url_option
def env_cmd(url: str | None, as_json: bool) -> None:
    """Show resolved configuration and active profiles."""
    if url:
        data = ActuatorClient(url).get("env")
    else:
        from pyfly.actuator.endpoints.env_endpoint import EnvEndpoint

        ctx = boot_context()
        data = run_async(EnvEndpoint(ctx).handle())
    _emit(data, as_json=as_json, title="Environment")


@click.command("health")
@_url_option
def health_cmd(url: str | None, as_json: bool) -> None:
    """Show application health."""
    if url:
        data = ActuatorClient(url).get("health")
    else:
        from pyfly.actuator.endpoints.health_endpoint import HealthEndpoint
        from pyfly.actuator.health import HealthAggregator

        ctx = boot_context()
        try:
            aggregator = ctx.get_bean(HealthAggregator)
        except Exception:  # noqa: BLE001 — actuator may be disabled; use a fresh aggregator
            aggregator = HealthAggregator()
        data = run_async(HealthEndpoint(aggregator).handle())
    _emit(data, as_json=as_json, title="Health")


@click.command("metrics")
@click.argument("name", required=False)
@_url_option
def metrics_cmd(name: str | None, url: str | None, as_json: bool) -> None:
    """List metrics (or one metric's detail)."""
    if url:
        data = ActuatorClient(url).get(f"metrics/{name}" if name else "metrics")
    else:
        from pyfly.actuator.endpoints import metrics_endpoint as _metrics_mod
        from pyfly.actuator.endpoints.metrics_endpoint import MetricsEndpoint

        if getattr(_metrics_mod, "REGISTRY", None) is None:
            err_console.print("[error]✗[/error] metrics require prometheus_client (install pyfly[observability]).")
            raise SystemExit(1)
        selector = {"selector": name} if name else None
        data = run_async(MetricsEndpoint().handle(selector))
    _emit(data, as_json=as_json, title="Metrics")


@click.command("actuator")
@click.argument("endpoint")
@_url_option
def actuator_cmd(endpoint: str, url: str | None, as_json: bool) -> None:
    """GET an arbitrary actuator endpoint (remote-only; requires --url)."""
    if not url:
        err_console.print("[error]✗[/error] 'actuator' requires --url (it queries a running app).")
        raise SystemExit(1)
    data = ActuatorClient(url).get(endpoint)
    _emit(data, as_json=as_json, title=f"actuator/{endpoint}")
