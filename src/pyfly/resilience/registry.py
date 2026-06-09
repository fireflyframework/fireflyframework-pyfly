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
"""ResilienceRegistry — config-driven registry of named resilience instances.

Materialises :class:`CircuitBreaker`, :class:`RateLimiter`, :class:`Bulkhead`,
and time-limiter timeouts from ``pyfly.resilience.*`` configuration keys,
providing parity with Resilience4j's named-registry model.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from pyfly.resilience.bulkhead import Bulkhead
from pyfly.resilience.circuit_breaker import CircuitBreaker
from pyfly.resilience.rate_limiter import RateLimiter

# Pattern: "5s" → 5 seconds, "1m" → 60 seconds, "500ms" → 0.5 seconds, "2.5" → 2.5 seconds
_DURATION_RE = re.compile(
    r"^\s*(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>ms|s|m|h)?\s*$",
    re.IGNORECASE,
)


def _parse_duration(raw: str | int | float) -> timedelta:
    """Parse a duration value into a :class:`~datetime.timedelta`.

    Accepts:
    * bare numbers (treated as seconds): ``5``, ``2.5``
    * strings with a unit suffix: ``"5s"``, ``"500ms"``, ``"1m"``, ``"2h"``
    * integers/floats (seconds)
    """
    if isinstance(raw, (int, float)):
        return timedelta(seconds=float(raw))
    m = _DURATION_RE.match(str(raw))
    if m is None:
        raise ValueError(f"Cannot parse duration {raw!r}; expected e.g. '5s', '500ms', '1m', '2h', or a number")
    value = float(m.group("value"))
    unit = (m.group("unit") or "s").lower()
    if unit == "ms":
        return timedelta(milliseconds=value)
    if unit == "s":
        return timedelta(seconds=value)
    if unit == "m":
        return timedelta(minutes=value)
    if unit == "h":
        return timedelta(hours=value)
    raise ValueError(f"Unknown duration unit {unit!r}")  # pragma: no cover


# Public alias — reused by other modules (e.g. the callbacks circuit-breaker config).
parse_duration = _parse_duration


class ResilienceRegistry:
    """Registry of named resilience instances built from configuration.

    Usage — inject the registry and look up instances by name::

        registry = ResilienceRegistry.from_config(config)

        cb = registry.circuit_breaker("payment-api")
        rl = registry.rate_limiter("search-api")
        bh = registry.bulkhead("db-pool")
        timeout = registry.time_limiter("slow-report")   # timedelta

    Unknown names raise :class:`KeyError`.
    """

    def __init__(
        self,
        circuit_breakers: dict[str, CircuitBreaker] | None = None,
        rate_limiters: dict[str, RateLimiter] | None = None,
        bulkheads: dict[str, Bulkhead] | None = None,
        time_limiters: dict[str, timedelta] | None = None,
    ) -> None:
        self._circuit_breakers: dict[str, CircuitBreaker] = circuit_breakers or {}
        self._rate_limiters: dict[str, RateLimiter] = rate_limiters or {}
        self._bulkheads: dict[str, Bulkhead] = bulkheads or {}
        self._time_limiters: dict[str, timedelta] = time_limiters or {}

    # ------------------------------------------------------------------
    # Typed accessors
    # ------------------------------------------------------------------

    def circuit_breaker(self, name: str) -> CircuitBreaker:
        """Return the :class:`CircuitBreaker` registered under *name*.

        Raises :class:`KeyError` if no circuit-breaker with that name exists.
        """
        try:
            return self._circuit_breakers[name]
        except KeyError:
            available = sorted(self._circuit_breakers)
            raise KeyError(f"No circuit-breaker named {name!r}. Available: {available or '(none)'}") from None

    def rate_limiter(self, name: str) -> RateLimiter:
        """Return the :class:`RateLimiter` registered under *name*.

        Raises :class:`KeyError` if no rate-limiter with that name exists.
        """
        try:
            return self._rate_limiters[name]
        except KeyError:
            available = sorted(self._rate_limiters)
            raise KeyError(f"No rate-limiter named {name!r}. Available: {available or '(none)'}") from None

    def bulkhead(self, name: str) -> Bulkhead:
        """Return the :class:`Bulkhead` registered under *name*.

        Raises :class:`KeyError` if no bulkhead with that name exists.
        """
        try:
            return self._bulkheads[name]
        except KeyError:
            available = sorted(self._bulkheads)
            raise KeyError(f"No bulkhead named {name!r}. Available: {available or '(none)'}") from None

    def time_limiter(self, name: str) -> timedelta:
        """Return the timeout :class:`~datetime.timedelta` registered under *name*.

        Raises :class:`KeyError` if no time-limiter with that name exists.
        """
        try:
            return self._time_limiters[name]
        except KeyError:
            available = sorted(self._time_limiters)
            raise KeyError(f"No time-limiter named {name!r}. Available: {available or '(none)'}") from None

    # ------------------------------------------------------------------
    # Convenience: list registered names
    # ------------------------------------------------------------------

    @property
    def circuit_breaker_names(self) -> list[str]:
        """Sorted list of registered circuit-breaker names."""
        return sorted(self._circuit_breakers)

    @property
    def rate_limiter_names(self) -> list[str]:
        """Sorted list of registered rate-limiter names."""
        return sorted(self._rate_limiters)

    @property
    def bulkhead_names(self) -> list[str]:
        """Sorted list of registered bulkhead names."""
        return sorted(self._bulkheads)

    @property
    def time_limiter_names(self) -> list[str]:
        """Sorted list of registered time-limiter names."""
        return sorted(self._time_limiters)

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls, config: Any) -> ResilienceRegistry:
        """Materialise a :class:`ResilienceRegistry` from a :class:`~pyfly.core.config.Config`.

        Reads the following nested config sections (all optional):

        ``pyfly.resilience.circuit-breaker.<name>``
            * ``failure-threshold`` (int, default 5)
            * ``recovery-timeout`` (duration, default ``"30s"``)
            * ``failure-rate-threshold`` (float 0–1, optional)
            * ``window-size`` (int, default 10)
            * ``half-open-max-calls`` (int, default 1)

        ``pyfly.resilience.rate-limiter.<name>``
            * ``max-tokens`` (int, default 10)
            * ``refill-rate`` (float tokens/s, default 10.0)

        ``pyfly.resilience.bulkhead.<name>``
            * ``max-concurrent`` (int, default 10)

        ``pyfly.resilience.time-limiter.<name>``
            * ``timeout`` (duration, default ``"30s"``)

        Config.get_section() is used to retrieve each sub-map; relaxed binding
        (kebab/snake interchangeable) is handled by Config internally.
        """
        circuit_breakers: dict[str, CircuitBreaker] = {}
        rate_limiters: dict[str, RateLimiter] = {}
        bulkheads: dict[str, Bulkhead] = {}
        time_limiters: dict[str, timedelta] = {}

        # --- circuit-breakers ---
        cb_section: dict[str, Any] = config.get_section("pyfly.resilience.circuit-breaker")
        for name, raw in cb_section.items():
            if not isinstance(raw, dict):
                continue
            params = {k.replace("-", "_"): v for k, v in raw.items()}
            failure_threshold = int(params.get("failure_threshold", 5))
            recovery_timeout_raw = params.get("recovery_timeout", "30s")
            recovery_timeout = _parse_duration(recovery_timeout_raw).total_seconds()
            failure_rate_threshold_raw = params.get("failure_rate_threshold")
            failure_rate_threshold = (
                float(failure_rate_threshold_raw) if failure_rate_threshold_raw is not None else None
            )
            window_size = int(params.get("window_size", 10))
            half_open_max_calls = int(params.get("half_open_max_calls", 1))
            circuit_breakers[name] = CircuitBreaker(
                failure_threshold=failure_threshold,
                recovery_timeout=recovery_timeout,
                failure_rate_threshold=failure_rate_threshold,
                window_size=window_size,
                half_open_max_calls=half_open_max_calls,
            )

        # --- rate-limiters ---
        rl_section: dict[str, Any] = config.get_section("pyfly.resilience.rate-limiter")
        for name, raw in rl_section.items():
            if not isinstance(raw, dict):
                continue
            params = {k.replace("-", "_"): v for k, v in raw.items()}
            max_tokens = int(params.get("max_tokens", 10))
            refill_rate = float(params.get("refill_rate", 10.0))
            rate_limiters[name] = RateLimiter(max_tokens=max_tokens, refill_rate=refill_rate)

        # --- bulkheads ---
        bh_section: dict[str, Any] = config.get_section("pyfly.resilience.bulkhead")
        for name, raw in bh_section.items():
            if not isinstance(raw, dict):
                continue
            params = {k.replace("-", "_"): v for k, v in raw.items()}
            max_concurrent = int(params.get("max_concurrent", 10))
            bulkheads[name] = Bulkhead(max_concurrent=max_concurrent)

        # --- time-limiters ---
        tl_section: dict[str, Any] = config.get_section("pyfly.resilience.time-limiter")
        for name, raw in tl_section.items():
            if not isinstance(raw, dict):
                continue
            params = {k.replace("-", "_"): v for k, v in raw.items()}
            timeout_raw = params.get("timeout", "30s")
            time_limiters[name] = _parse_duration(timeout_raw)

        return cls(
            circuit_breakers=circuit_breakers,
            rate_limiters=rate_limiters,
            bulkheads=bulkheads,
            time_limiters=time_limiters,
        )
