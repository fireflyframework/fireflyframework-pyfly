# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Native, immutable text-rate quotes without usage probes or provider inference."""

from dataclasses import FrozenInstanceError, asdict, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from genai_prices import data_snapshot, types

from pyfly.agentic import observability
from pyfly.agentic.observability.cost_resolvers import CostContext, UnknownModelCostError, resolve_cost

AT = datetime(2026, 10, 31, 12, tzinfo=UTC)


def quote(model: str, **kwargs: Any) -> Any:
    function = getattr(observability, "quote_model_price", None)
    assert callable(function), "Public native quote_model_price API is required"
    return function(model, **kwargs)


def snapshot(monkeypatch: pytest.MonkeyPatch, prices: Any, **kwargs: Any) -> tuple[Any, Any]:
    model = types.ModelInfo(id="fixture-model", match=types.ClauseEquals("fixture-model"), prices=prices, **kwargs)
    provider = types.Provider(id="openai", name="Fixture", api_pattern="https://fixture.invalid", models=[model])
    value = data_snapshot.DataSnapshot(providers=[provider], from_auto_update=False, timestamp=AT)
    monkeypatch.setattr(data_snapshot, "get_snapshot", lambda: value)
    return value, model


def test_framework_quote_requires_first_party_identity_and_has_exact_decimal_rates() -> None:
    value = quote("anthropic:claude-opus-5", at=AT)
    assert value.provider == "anthropic" and value.model == "claude-opus-5"
    assert value.input_per_million == Decimal("5") and value.output_per_million == Decimal("25")
    assert type(value.input_per_million) is type(value.output_per_million) is Decimal
    assert value.currency == "USD" and value.basis == "text_token_rates"
    assert value.source == "framework_table" and value.source_version
    assert value.at == AT and value.at.tzinfo is UTC
    assert len(value.source_digest) == 64


@pytest.mark.parametrize("provider", [None, "ollama", "local", "openai-compatible", "openai-chat", "openai-responses"])
def test_missing_local_or_protocol_identity_does_not_inherit_a_vendor_price(provider: str | None) -> None:
    assert quote("claude-opus-5", provider=provider, at=AT) is None


def test_explicit_provider_never_uses_another_providers_framework_row(monkeypatch: pytest.MonkeyPatch) -> None:
    _, model = snapshot(monkeypatch, types.ModelPrice(input_mtok=Decimal("0.5"), output_mtok=Decimal("1")))
    model.id, model.match = "claude-opus-5", types.ClauseEquals("claude-opus-5")
    value = quote("claude-opus-5", provider="openai", at=AT)
    assert value.provider == "openai" and value.source == "genai_prices"
    assert value.input_per_million == Decimal("0.5")


def test_genai_quote_reads_public_rates_without_a_calculation_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot(monkeypatch, types.ModelPrice(input_mtok=Decimal("2.35"), output_mtok=Decimal("4.75")))
    monkeypatch.setattr(types.ModelPrice, "calc_price", lambda *args: pytest.fail("No usage probe is allowed"))
    value = quote("fixture-model", provider="openai", at=AT)
    assert value.input_per_million == Decimal("2.35") and value.output_per_million == Decimal("4.75")
    assert value.source == "genai_prices" and value.source_version
    assert value.model == "fixture-model" and value.provider == "openai"


def test_explicit_zero_rates_are_preserved_as_known_prices(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot(monkeypatch, types.ModelPrice(input_mtok=Decimal("0"), output_mtok=Decimal("0")))
    value = quote("fixture-model", provider="openai", at=AT)
    assert value.input_per_million == value.output_per_million == Decimal("0")


@pytest.mark.parametrize("invalid", [True, 1.25, Decimal("-1"), Decimal("NaN"), Decimal("Infinity")])
def test_public_quote_requires_finite_nonnegative_decimal_rates(invalid: Any) -> None:
    value = quote("claude-opus-5", provider="anthropic", at=AT)
    with pytest.raises(ValueError):
        replace(value, input_per_million=invalid)


@pytest.mark.parametrize(
    "input_rate,output_rate",
    [
        (None, Decimal("2")),
        (Decimal("1"), None),
        (Decimal("-1"), Decimal("2")),
        (Decimal("NaN"), Decimal("2")),
        (Decimal("Infinity"), Decimal("2")),
        (True, Decimal("2")),
        (1.5, Decimal("2")),
        (types.TieredPrices(base=Decimal("1"), tiers=[]), Decimal("2")),
    ],
)
def test_missing_invalid_or_usage_tiered_rates_require_fallback(
    monkeypatch: pytest.MonkeyPatch, input_rate: Any, output_rate: Any
) -> None:
    snapshot(monkeypatch, types.ModelPrice(input_mtok=input_rate, output_mtok=output_rate))
    assert quote("fixture-model", provider="openai", at=AT) is None


def test_public_date_and_context_selection_produces_effective_scalar_rates(monkeypatch: pytest.MonkeyPatch) -> None:
    prices = [
        types.ConditionalPrice(prices=types.ModelPrice(input_mtok=Decimal("1"), output_mtok=Decimal("2"))),
        types.ConditionalPrice(
            types.StartDateConstraint(date(2026, 10, 1)),
            prices=types.ModelPrice(input_mtok=Decimal("3"), output_mtok=Decimal("4")),
        ),
    ]
    snapshot(
        monkeypatch,
        prices,
        price_variants=[
            types.PriceVariant(
                when={"service_tier": "flex"},
                prices=types.ModelPrice(input_mtok=Decimal("0.5"), output_mtok=Decimal("1")),
            )
        ],
    )
    earlier = quote("fixture-model", provider="openai", at=datetime(2026, 9, 1, tzinfo=UTC))
    current = quote("fixture-model", provider="openai", at=AT)
    selected = quote(
        "fixture-model",
        provider="openai",
        at=AT,
        price_context={"service_tier": "flex", "private": "fixture-sensitive-context"},
    )
    assert (earlier.input_per_million, current.input_per_million, selected.input_per_million) == (
        Decimal("1"),
        Decimal("3"),
        Decimal("0.5"),
    )
    assert len({earlier.source_digest, current.source_digest, selected.source_digest}) == 3
    assert "fixture-sensitive-context" not in repr(selected) and "price_context" not in asdict(selected)


def test_quote_is_frozen_and_does_not_change_with_its_source_row(monkeypatch: pytest.MonkeyPatch) -> None:
    data, model = snapshot(monkeypatch, types.ModelPrice(input_mtok=Decimal("1"), output_mtok=Decimal("2")))
    first = quote("fixture-model", provider="openai", at=AT)
    with pytest.raises(FrozenInstanceError):
        first.input_per_million = Decimal("99")
    model.prices.input_mtok = Decimal("3")
    second = quote("fixture-model", provider="openai", at=AT)
    assert first.input_per_million == Decimal("1") and second.input_per_million == Decimal("3")
    assert first.source_digest != second.source_digest
    data.from_auto_update = True
    third = quote("fixture-model", provider="openai", at=AT)
    assert third.source_digest != second.source_digest


def test_provider_fallback_cannot_borrow_another_providers_model_row(monkeypatch: pytest.MonkeyPatch) -> None:
    data, _ = snapshot(monkeypatch, types.ModelPrice(input_mtok=Decimal("1"), output_mtok=Decimal("2")))
    data.providers.append(
        types.Provider(
            id="fixture-proxy", name="Proxy", api_pattern="https://proxy.invalid", fallback_model_providers=["openai"]
        )
    )
    assert quote("fixture-model", provider="fixture-proxy", at=AT) is None


def test_unknown_or_conflicting_identity_and_unavailable_data_have_no_quote(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot(monkeypatch, types.ModelPrice(input_mtok=Decimal("1"), output_mtok=Decimal("2")))
    assert quote("missing", provider="openai", at=AT) is None
    assert quote("anthropic:fixture-model", provider="openai", at=AT) is None
    monkeypatch.setattr(data_snapshot, "get_snapshot", lambda: (_ for _ in ()).throw(RuntimeError("unavailable")))
    assert quote("fixture-model", provider="openai", at=AT) is None


def test_quote_defaults_to_aware_utc_and_rejects_naive_timestamps() -> None:
    assert quote("claude-opus-5", provider="anthropic").at.tzinfo is UTC
    assert quote("claude-opus-5", provider="anthropic", at=datetime(2026, 10, 31)) is None


@pytest.mark.parametrize("context", [[], True, {"service_tier": True}])
def test_unsupported_price_context_returns_no_quote(context: Any) -> None:
    assert quote("claude-opus-5", provider="anthropic", at=AT, price_context=context) is None


@pytest.mark.parametrize("invalid", [True, False, -1.0, float("nan"), float("inf"), float("-inf"), "1.25"])
def test_invalid_scalar_cost_results_do_not_stop_a_valid_resolver_chain(invalid: Any) -> None:
    context = CostContext(model="fixture:model", input_tokens=1, output_tokens=1)
    assert resolve_cost(context, [lambda _: invalid, lambda _: 0.5]) == 0.5
    assert resolve_cost(context, [lambda _: invalid], strict=False) is None
    with pytest.raises(UnknownModelCostError):
        resolve_cost(context, [lambda _: invalid], strict=True)


def test_valid_zero_and_decimal_cost_results_remain_supported() -> None:
    context = CostContext(model="fixture:model", input_tokens=1, output_tokens=1)
    assert resolve_cost(context, [lambda _: 0.0], strict=True) == 0.0
    assert resolve_cost(context, [lambda _: Decimal("1.25")]) == 1.25
