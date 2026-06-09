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
"""Tests for individual generate subcommands."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from pyfly.cli.generate import generate_group


def scaffold(root: Path, package: str = "shop", archetype: str = "web-api") -> None:
    (root / "src" / package).mkdir(parents=True)
    (root / "src" / package / "__init__.py").write_text("")
    (root / "tests").mkdir()
    (root / "pyproject.toml").write_text(f'[project]\nname = "{package}"\n')
    (root / "pyfly.yaml").write_text(
        f"pyfly:\n  app:\n    name: {package}\n    archetype: {archetype}\n"
    )


def run(args: list[str], cwd: Path):
    return CliRunner().invoke(generate_group, args, obj={"cwd": cwd})


class TestControllerGenerator:
    def test_rest_controller(self, tmp_path: Path) -> None:
        scaffold(tmp_path, archetype="web-api")
        result = run(["controller", "Order"], tmp_path)
        assert result.exit_code == 0, result.output
        f = tmp_path / "src" / "shop" / "controllers" / "order_controller.py"
        text = f.read_text()
        assert "@rest_controller" in text
        assert '@request_mapping("/orders")' in text
        assert "class OrderController" in text
        assert (tmp_path / "tests" / "test_order_controller.py").exists()

    def test_web_controller_for_web_archetype(self, tmp_path: Path) -> None:
        scaffold(tmp_path, archetype="web")
        result = run(["controller", "Page"], tmp_path)
        assert result.exit_code == 0, result.output
        text = (tmp_path / "src" / "shop" / "controllers" / "page_controller.py").read_text()
        assert "@controller" in text
        assert "Jinja2Templates" in text


class TestEntityRepository:
    def test_entity_plain_when_no_data(self, tmp_path: Path) -> None:
        scaffold(tmp_path, archetype="web-api")
        result = run(["entity", "Product"], tmp_path)
        assert result.exit_code == 0, result.output
        text = (tmp_path / "src" / "shop" / "models" / "product.py").read_text()
        assert "class Product(BaseModel)" in text

    def test_entity_sqlalchemy_when_data(self, tmp_path: Path) -> None:
        scaffold(tmp_path, archetype="web-api")
        (tmp_path / "pyfly.yaml").write_text(
            "pyfly:\n  app:\n    name: shop\n    archetype: web-api\n"
            "  data:\n    relational:\n      enabled: true\n"
        )
        result = run(["entity", "Product"], tmp_path)
        assert result.exit_code == 0, result.output
        text = (tmp_path / "src" / "shop" / "models" / "product.py").read_text()
        assert "class Product(Base)" in text
        assert '__tablename__ = "products"' in text

    def test_repository_data(self, tmp_path: Path) -> None:
        scaffold(tmp_path, archetype="web-api")
        (tmp_path / "pyfly.yaml").write_text(
            "pyfly:\n  app:\n    name: shop\n    archetype: web-api\n"
            "  data:\n    relational:\n      enabled: true\n"
        )
        result = run(["repository", "Product"], tmp_path)
        assert result.exit_code == 0, result.output
        text = (tmp_path / "src" / "shop" / "repositories" / "product_repository.py").read_text()
        assert "class ProductRepository(Repository[Product, int])" in text
        assert "@repository" in text


class TestDtoAggregate:
    def test_dto(self, tmp_path: Path) -> None:
        scaffold(tmp_path)
        result = run(["dto", "Order"], tmp_path)
        assert result.exit_code == 0, result.output
        text = (tmp_path / "src" / "shop" / "dto" / "order_dto.py").read_text()
        assert "class OrderCreateRequest(BaseModel)" in text
        assert "class OrderResponse(BaseModel)" in text

    def test_aggregate(self, tmp_path: Path) -> None:
        scaffold(tmp_path, archetype="hexagonal")
        result = run(["aggregate", "Wallet"], tmp_path)
        assert result.exit_code == 0, result.output
        text = (tmp_path / "src" / "shop" / "domain" / "wallet.py").read_text()
        assert "class Wallet" in text
        assert "_events" in text


class TestCqrs:
    def test_command_and_handler(self, tmp_path: Path) -> None:
        scaffold(tmp_path)
        result = run(["command", "OpenWallet"], tmp_path)
        assert result.exit_code == 0, result.output
        cmd = (tmp_path / "src" / "shop" / "cqrs" / "open_wallet_command.py").read_text()
        handler = (tmp_path / "src" / "shop" / "cqrs" / "open_wallet_handler.py").read_text()
        assert "class OpenWallet(Command[str])" in cmd
        assert "from pyfly.cqrs import Command" in cmd
        assert "class OpenWalletHandler(CommandHandler[OpenWallet, str])" in handler
        assert "@command_handler" in handler
        assert "async def do_handle" in handler

    def test_query_and_handler(self, tmp_path: Path) -> None:
        scaffold(tmp_path)
        result = run(["query", "GetWallet"], tmp_path)
        assert result.exit_code == 0, result.output
        q = (tmp_path / "src" / "shop" / "cqrs" / "get_wallet_query.py").read_text()
        h = (tmp_path / "src" / "shop" / "cqrs" / "get_wallet_handler.py").read_text()
        assert "class GetWallet(Query[dict | None])" in q
        assert "class GetWalletHandler(QueryHandler[GetWallet, dict | None])" in h
        assert "@query_handler" in h


class TestEvent:
    def test_event_and_listener(self, tmp_path: Path) -> None:
        scaffold(tmp_path)
        result = run(["event", "OrderPlaced"], tmp_path)
        assert result.exit_code == 0, result.output
        ev = (tmp_path / "src" / "shop" / "events" / "order_placed_event.py").read_text()
        ls = (tmp_path / "src" / "shop" / "events" / "order_placed_listener.py").read_text()
        assert "class OrderPlaced(DomainEvent)" in ev
        assert "from pyfly.domain import DomainEvent" in ev
        assert '@event_listener(event_types=["OrderPlaced"])' in ls
        assert "EventEnvelope" in ls


class TestSaga:
    def test_saga(self, tmp_path: Path) -> None:
        scaffold(tmp_path)
        result = run(["saga", "MoneyTransfer"], tmp_path)
        assert result.exit_code == 0, result.output
        text = (tmp_path / "src" / "shop" / "sagas" / "money_transfer_saga.py").read_text()
        assert '@saga(name="money-transfer")' in text
        assert "@saga_step(" in text
        assert "compensate=" in text


class TestScheduled:
    def test_scheduled(self, tmp_path: Path) -> None:
        scaffold(tmp_path)
        result = run(["scheduled", "ReportJob"], tmp_path)
        assert result.exit_code == 0, result.output
        text = (tmp_path / "src" / "shop" / "jobs" / "report_job_job.py").read_text()
        assert "from pyfly.scheduling import scheduled" in text
        assert "@scheduled(" in text
        assert "async def run" in text
