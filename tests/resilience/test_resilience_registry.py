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
"""Tests for ResilienceRegistry and ResilienceAutoConfiguration."""

from __future__ import annotations

from datetime import timedelta

import pytest

from pyfly.core.config import Config
from pyfly.resilience import ResilienceRegistry
from pyfly.resilience.bulkhead import Bulkhead
from pyfly.resilience.circuit_breaker import CircuitBreaker
from pyfly.resilience.rate_limiter import RateLimiter
from pyfly.resilience.registry import _parse_duration  # type: ignore[reportPrivateUsage]

# ---------------------------------------------------------------------------
# Duration parser
# ---------------------------------------------------------------------------


class TestParseDuration:
    def test_bare_int(self) -> None:
        assert _parse_duration(5) == timedelta(seconds=5)

    def test_bare_float(self) -> None:
        assert _parse_duration(2.5) == timedelta(seconds=2.5)

    def test_seconds_suffix(self) -> None:
        assert _parse_duration("5s") == timedelta(seconds=5)

    def test_milliseconds_suffix(self) -> None:
        assert _parse_duration("500ms") == timedelta(milliseconds=500)

    def test_minutes_suffix(self) -> None:
        assert _parse_duration("2m") == timedelta(minutes=2)

    def test_hours_suffix(self) -> None:
        assert _parse_duration("1h") == timedelta(hours=1)

    def test_no_unit_defaults_to_seconds(self) -> None:
        assert _parse_duration("10") == timedelta(seconds=10)

    def test_invalid_raises(self) -> None:
        with pytest.raises(ValueError, match="Cannot parse duration"):
            _parse_duration("five seconds")


# ---------------------------------------------------------------------------
# ResilienceRegistry — from_config materialisation
# ---------------------------------------------------------------------------


def _make_config(data: dict) -> Config:  # type: ignore[type-arg]
    """Build a Config directly from a nested dict (no file I/O)."""
    return Config(data)


class TestResilienceRegistryFromConfig:
    def test_circuit_breaker_materialised(self) -> None:
        cfg = _make_config(
            {
                "pyfly": {
                    "resilience": {
                        "circuit-breaker": {
                            "payment-api": {
                                "failure-threshold": 3,
                                "recovery-timeout": "10s",
                                "window-size": 8,
                                "half-open-max-calls": 2,
                            }
                        }
                    }
                }
            }
        )
        registry = ResilienceRegistry.from_config(cfg)
        cb = registry.circuit_breaker("payment-api")
        assert isinstance(cb, CircuitBreaker)
        assert cb.failure_threshold == 3
        assert cb.recovery_timeout == 10.0
        assert cb.window_size == 8
        assert cb.half_open_max_calls == 2

    def test_circuit_breaker_failure_rate_threshold(self) -> None:
        cfg = _make_config(
            {
                "pyfly": {
                    "resilience": {
                        "circuit-breaker": {
                            "svc": {
                                "failure-rate-threshold": 0.5,
                            }
                        }
                    }
                }
            }
        )
        cb = ResilienceRegistry.from_config(cfg).circuit_breaker("svc")
        assert cb.failure_rate_threshold == pytest.approx(0.5)

    def test_rate_limiter_materialised(self) -> None:
        cfg = _make_config(
            {
                "pyfly": {
                    "resilience": {
                        "rate-limiter": {
                            "search-api": {
                                "max-tokens": 200,
                                "refill-rate": 100.0,
                            }
                        }
                    }
                }
            }
        )
        registry = ResilienceRegistry.from_config(cfg)
        rl = registry.rate_limiter("search-api")
        assert isinstance(rl, RateLimiter)
        assert rl._max_tokens == 200
        assert rl._refill_rate == pytest.approx(100.0)

    def test_bulkhead_materialised(self) -> None:
        cfg = _make_config(
            {
                "pyfly": {
                    "resilience": {
                        "bulkhead": {
                            "db-pool": {
                                "max-concurrent": 5,
                            }
                        }
                    }
                }
            }
        )
        registry = ResilienceRegistry.from_config(cfg)
        bh = registry.bulkhead("db-pool")
        assert isinstance(bh, Bulkhead)
        assert bh.max_concurrent == 5

    def test_time_limiter_materialised(self) -> None:
        cfg = _make_config(
            {
                "pyfly": {
                    "resilience": {
                        "time-limiter": {
                            "slow-report": {
                                "timeout": "30s",
                            }
                        }
                    }
                }
            }
        )
        registry = ResilienceRegistry.from_config(cfg)
        td = registry.time_limiter("slow-report")
        assert isinstance(td, timedelta)
        assert td == timedelta(seconds=30)

    def test_time_limiter_milliseconds(self) -> None:
        cfg = _make_config(
            {
                "pyfly": {
                    "resilience": {
                        "time-limiter": {
                            "fast-op": {
                                "timeout": "500ms",
                            }
                        }
                    }
                }
            }
        )
        td = ResilienceRegistry.from_config(cfg).time_limiter("fast-op")
        assert td == timedelta(milliseconds=500)

    def test_multiple_named_instances(self) -> None:
        cfg = _make_config(
            {
                "pyfly": {
                    "resilience": {
                        "rate-limiter": {
                            "default": {"max-tokens": 100, "refill-rate": 50.0},
                            "payment-api": {"max-tokens": 10, "refill-rate": 2.0},
                        },
                        "bulkhead": {
                            "default": {"max-concurrent": 20},
                            "db-pool": {"max-concurrent": 5},
                        },
                    }
                }
            }
        )
        registry = ResilienceRegistry.from_config(cfg)
        assert registry.rate_limiter("default")._max_tokens == 100
        assert registry.rate_limiter("payment-api")._max_tokens == 10
        assert registry.bulkhead("default").max_concurrent == 20
        assert registry.bulkhead("db-pool").max_concurrent == 5

    def test_empty_config_gives_empty_registry(self) -> None:
        """No resilience keys → empty registry, no error."""
        registry = ResilienceRegistry.from_config(Config({}))
        assert registry.circuit_breaker_names == []
        assert registry.rate_limiter_names == []
        assert registry.bulkhead_names == []
        assert registry.time_limiter_names == []

    def test_missing_resilience_section_gives_empty_registry(self) -> None:
        registry = ResilienceRegistry.from_config(Config({"pyfly": {"app": {"name": "test"}}}))
        assert registry.bulkhead_names == []

    def test_unknown_circuit_breaker_raises_key_error(self) -> None:
        registry = ResilienceRegistry.from_config(Config({}))
        with pytest.raises(KeyError, match="No circuit-breaker named 'missing'"):
            registry.circuit_breaker("missing")

    def test_unknown_rate_limiter_raises_key_error(self) -> None:
        registry = ResilienceRegistry.from_config(Config({}))
        with pytest.raises(KeyError, match="No rate-limiter named 'missing'"):
            registry.rate_limiter("missing")

    def test_unknown_bulkhead_raises_key_error(self) -> None:
        registry = ResilienceRegistry.from_config(Config({}))
        with pytest.raises(KeyError, match="No bulkhead named 'missing'"):
            registry.bulkhead("missing")

    def test_unknown_time_limiter_raises_key_error(self) -> None:
        registry = ResilienceRegistry.from_config(Config({}))
        with pytest.raises(KeyError, match="No time-limiter named 'missing'"):
            registry.time_limiter("missing")

    def test_error_message_lists_available_names(self) -> None:
        cfg = _make_config(
            {
                "pyfly": {
                    "resilience": {
                        "bulkhead": {
                            "alpha": {"max-concurrent": 2},
                            "beta": {"max-concurrent": 3},
                        }
                    }
                }
            }
        )
        registry = ResilienceRegistry.from_config(cfg)
        with pytest.raises(KeyError, match="alpha"):
            registry.bulkhead("unknown")


# ---------------------------------------------------------------------------
# ResilienceRegistry — direct construction
# ---------------------------------------------------------------------------


class TestResilienceRegistryDirect:
    def test_direct_construction_and_lookup(self) -> None:
        cb = CircuitBreaker(failure_threshold=7)
        registry = ResilienceRegistry(circuit_breakers={"svc": cb})
        assert registry.circuit_breaker("svc") is cb

    def test_name_lists_sorted(self) -> None:
        rl1 = RateLimiter(max_tokens=10, refill_rate=1.0)
        rl2 = RateLimiter(max_tokens=20, refill_rate=2.0)
        registry = ResilienceRegistry(rate_limiters={"zebra": rl1, "alpha": rl2})
        assert registry.rate_limiter_names == ["alpha", "zebra"]


# ---------------------------------------------------------------------------
# Auto-configuration bean
# ---------------------------------------------------------------------------


class TestResilienceAutoConfiguration:
    def test_bean_returns_registry_wired_from_config(self) -> None:
        from pyfly.resilience.auto_configuration import ResilienceAutoConfiguration

        cfg = _make_config(
            {
                "pyfly": {
                    "resilience": {
                        "bulkhead": {
                            "my-svc": {"max-concurrent": 7},
                        }
                    }
                }
            }
        )
        auto_cfg = ResilienceAutoConfiguration()
        registry = auto_cfg.resilience_registry(cfg)
        assert isinstance(registry, ResilienceRegistry)
        assert registry.bulkhead("my-svc").max_concurrent == 7

    def test_bean_returns_empty_registry_when_no_resilience_config(self) -> None:
        from pyfly.resilience.auto_configuration import ResilienceAutoConfiguration

        auto_cfg = ResilienceAutoConfiguration()
        registry = auto_cfg.resilience_registry(Config({}))
        assert isinstance(registry, ResilienceRegistry)
        assert registry.bulkhead_names == []
