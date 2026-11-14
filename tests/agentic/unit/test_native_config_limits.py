"""Native binding must preserve the model names used for quota admission."""

import pytest

from pyfly.agentic.auto_configuration import bind_agentic_config
from pyfly.agentic.exceptions import RateLimitError
from pyfly.agentic.observability.quota import QuotaManager
from pyfly.core.config import Config


def test_bound_literal_model_limit_rejects_third_request(monkeypatch):
    monkeypatch.setenv("TEST_AGENTIC_LIMIT", "2")
    config = bind_agentic_config(
        Config(
            {
                "pyfly": {
                    "agentic": {
                        "quota-enabled": True,
                        "quota-rate-limits": {
                            "openai:gpt-4o": "${TEST_AGENTIC_LIMIT}",
                        },
                    }
                }
            }
        )
    )
    quota = QuotaManager(rate_limits=config.quota_rate_limits)
    quota.reserve_request("openai:gpt-4o")
    quota.reserve_request("openai:gpt-4o")
    with pytest.raises(RateLimitError):
        quota.reserve_request("openai:gpt-4o")
    monkeypatch.setenv("PYFLY_AGENTIC_QUOTA_RATE_LIMITS", '{"openrouter:vendor/model-a": 2}')
    config = bind_agentic_config(Config({"pyfly": {"agentic": {"quota-enabled": True}}}))
    assert config.quota_enabled is True
    assert config.quota_rate_limits == {"openrouter:vendor/model-a": 2}
