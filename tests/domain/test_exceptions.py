# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Tests for :mod:`pyfly.domain.exceptions`."""

from __future__ import annotations

import pytest

from pyfly.domain import AggregateNotFound, BusinessRuleViolation, DomainException
from pyfly.kernel import BusinessException


def test_domain_exception_is_a_business_exception() -> None:
    assert issubclass(DomainException, BusinessException)


def test_business_rule_violation_carries_rule_in_context() -> None:
    with pytest.raises(BusinessRuleViolation) as exc_info:
        raise BusinessRuleViolation("orders-cannot-ship-twice")
    err = exc_info.value
    assert err.rule == "orders-cannot-ship-twice"
    assert err.code == "DOMAIN_RULE_VIOLATION"
    assert err.context["rule"] == "orders-cannot-ship-twice"
    assert "orders-cannot-ship-twice" in str(err)


def test_business_rule_violation_accepts_custom_message_and_code() -> None:
    err = BusinessRuleViolation(
        "must-be-active",
        message="Account is closed",
        code="ACCT_CLOSED",
        context={"account_id": "a-1"},
    )
    assert str(err) == "Account is closed"
    assert err.code == "ACCT_CLOSED"
    assert err.context == {"account_id": "a-1", "rule": "must-be-active"}


def test_aggregate_not_found_carries_type_and_id() -> None:
    err = AggregateNotFound("Order", "o-1")
    assert err.aggregate_type == "Order"
    assert err.aggregate_id == "o-1"
    assert err.code == "DOMAIN_AGGREGATE_NOT_FOUND"
    assert err.context == {"aggregate_type": "Order", "id": "o-1"}
    assert "Order" in str(err)
    assert "o-1" in str(err)
