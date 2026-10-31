# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Frozen base text-token rates for pre-effect pricing decisions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from importlib.metadata import version
from typing import Literal, get_args

from genai_prices import data_snapshot, types

from pyfly import __version__
from pyfly.agentic.observability.cost_resolvers import FRAMEWORK_PRICE_TABLE


@dataclass(frozen=True)
class ModelPriceQuote:
    """Effective USD input/output rates per million text tokens, not a full invoice.

    ``source_digest`` identifies the selected effective source row, not an entire
    upstream dataset. Cache, tools, audio, images and other charges are outside
    this quote. Context values and credential material are not retained.
    """

    provider: str
    model: str
    input_per_million: Decimal
    output_per_million: Decimal
    source: Literal["framework_table", "genai_prices"]
    source_version: str
    source_digest: str
    at: datetime
    currency: Literal["USD"] = "USD"
    basis: Literal["text_token_rates"] = "text_token_rates"

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (self.provider, self.model, self.source_version)
        ):
            raise ValueError("Price quotes require provider, model and source version")
        for rate in (self.input_per_million, self.output_per_million):
            if not isinstance(rate, Decimal) or not rate.is_finite() or rate < 0:
                raise ValueError("Text-token rates must be finite non-negative Decimals")
        if (
            self.currency != "USD"
            or self.basis != "text_token_rates"
            or self.source not in {"framework_table", "genai_prices"}
        ):
            raise ValueError("Unsupported price quote currency, basis or source")
        if len(self.source_digest) != 64 or any(
            character not in "0123456789abcdef" for character in self.source_digest
        ):
            raise ValueError("A selected-row SHA256 digest is required")
        if not isinstance(self.at, datetime) or self.at.tzinfo is None or self.at.utcoffset() is None:
            raise ValueError("A timezone-aware quote timestamp is required")
        object.__setattr__(self, "at", self.at.astimezone(UTC))


_PROTOCOL_OR_LOCAL = frozenset(
    {
        "local",
        "ollama",
        "test",
        "function",
        "unknown",
        "openai-compatible",
        "openai-chat",
        "openai-responses",
        "azure-chat",
        "azure-responses",
        "litellm",
    }
)


def _identity(model: str, provider: str | None) -> tuple[str, str] | None:
    if not isinstance(model, str) or not model or model != model.strip():
        return None
    prefix, separator, reference = model.partition(":")
    if provider is None:
        if not separator:
            return None
        provider, model = prefix, reference
    elif not isinstance(provider, str):
        return None
    else:
        known = set(get_args(types.ProviderID)) | _PROTOCOL_OR_LOCAL
        if separator and prefix.lower() in known:
            if prefix.lower() != provider.lower():
                return None
            model = reference
    provider = provider.lower()
    if not provider or provider != provider.strip() or provider in _PROTOCOL_OR_LOCAL or not model:
        return None
    return provider, model


def _quote(
    provider: str,
    model: str,
    input_rate: Decimal,
    output_rate: Decimal,
    source: Literal["framework_table", "genai_prices"],
    source_version: str,
    at: datetime,
    snapshot: str | None = None,
) -> ModelPriceQuote:
    row = {
        "provider": provider,
        "model": model,
        "input_per_million": str(input_rate),
        "output_per_million": str(output_rate),
        "currency": "USD",
        "basis": "text_token_rates",
        "source": source,
        "source_version": source_version,
        "auto_update_timestamp": snapshot,
    }
    digest = hashlib.sha256(json.dumps(row, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return ModelPriceQuote(provider, model, input_rate, output_rate, source, source_version, digest, at)


def quote_model_price(
    model: str,
    *,
    provider: str | None = None,
    at: datetime | None = None,
    price_context: Mapping[str, str] | None = None,
) -> ModelPriceQuote | None:
    """Quote scalar base text rates from an explicit pricing identity, else None.

    Supply ``provider`` or an explicit ``provider:model`` string. Transport aliases,
    local providers and cross-provider fallback rows are not pricing identities.
    Framework rows apply only to first-party Anthropic base pricing. Public
    genai-prices timestamp/context selection is supported when both resulting
    text rates are finite non-negative Decimals. Usage tiers and missing prices
    require a caller-owned fallback; no scalar resolver or usage probe is used.
    """
    identity = _identity(model, provider)
    if identity is None:
        return None
    provider, model = identity
    instant = at if at is not None else datetime.now(UTC)
    if not isinstance(instant, datetime) or instant.tzinfo is None or instant.utcoffset() is None:
        return None
    instant = instant.astimezone(UTC)
    if price_context is not None and (
        not isinstance(price_context, Mapping)
        or any(not isinstance(key, str) or not isinstance(value, str) for key, value in price_context.items())
    ):
        return None
    try:
        if provider == "anthropic" and not price_context:
            reference = model.lower()
            for prefix, rate in FRAMEWORK_PRICE_TABLE.items():
                if reference == prefix or reference.startswith((prefix + "-", prefix + "@")):
                    values = rate.input_per_million, rate.output_per_million
                    if any(isinstance(value, bool) or not isinstance(value, (int, float, Decimal)) for value in values):
                        return None
                    return _quote(
                        provider,
                        prefix,
                        Decimal(str(values[0])),
                        Decimal(str(values[1])),
                        "framework_table",
                        __version__,
                        instant,
                    )
        data = data_snapshot.get_snapshot()
        resolved_provider, resolved_model = data.find_provider_model(model, None, provider, None)
        if resolved_provider.id != provider or not any(resolved_model is item for item in resolved_provider.models):
            return None
        prices = resolved_model.get_prices(instant, dict(price_context) if price_context is not None else None)
        if not isinstance(prices.input_mtok, Decimal) or not isinstance(prices.output_mtok, Decimal):
            return None
        return _quote(
            resolved_provider.id,
            resolved_model.id,
            prices.input_mtok,
            prices.output_mtok,
            "genai_prices",
            version("genai-prices"),
            instant,
            data.timestamp.isoformat() if data.from_auto_update else None,
        )
    except Exception:  # noqa: BLE001
        return None
