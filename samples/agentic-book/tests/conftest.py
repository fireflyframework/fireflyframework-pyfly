"""Prohibit accidentally introducing a provider-backed model into offline tests."""

import pytest
from pydantic_ai import models


@pytest.fixture(autouse=True)
def prohibit_real_model_requests(monkeypatch):
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", False)
