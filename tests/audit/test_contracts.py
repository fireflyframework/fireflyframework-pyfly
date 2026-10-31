# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from pyfly.audit import AuditActor, AuditEvent, AuditResource, AuditScope, canonical_bytes


def event(**values: object) -> AuditEvent:
    return AuditEvent(
        event_id="event-1",
        event_type="run.admitted",
        scope=AuditScope(tenant_id="local", workspace_id="studio", project_id="default"),
        actor=AuditActor(kind="human", subject_id="user-1"),
        resource=AuditResource(kind="run", resource_id="run-1", revision=1),
        occurred_at=datetime(2026, 10, 31, tzinfo=UTC),
        outcome="admitted",
        **values,
    )


def test_events_reject_content_and_validation_errors_hide_rejected_values() -> None:
    for value in ({"detail": {"prompt": "SECRET_CANARY"}}, {"operation_id": "https://SECRET_CANARY"}):
        with pytest.raises(ValidationError) as failed:
            event(**value)
        assert "SECRET_CANARY" not in str(failed.value)
    with pytest.raises(ValidationError):
        event().actor.subject_id = "another-user"
    with pytest.raises(ValidationError):
        event().outcome = "failed"


def test_canonical_bytes_are_stable_ascii_json_subset_not_jcs() -> None:
    assert canonical_bytes({"z": "é", "a": [1, True, None]}) == b'{"a":[1,true,null],"z":"\\u00e9"}'
    with pytest.raises(ValueError):
        canonical_bytes({"a": float("nan")})
    with pytest.raises(ValueError):
        canonical_bytes({"a": 0.25})


def test_same_instant_is_normalized_for_idempotency() -> None:
    from datetime import timedelta, timezone

    first = event()
    second = first.model_copy(update={"occurred_at": datetime(2026, 10, 31, 1, tzinfo=timezone(timedelta(hours=1)))})
    validated = AuditEvent.model_validate_json(second.model_dump_json())
    assert first.model_dump_json() == validated.model_dump_json()


def test_checkpoint_binds_scope_and_journal_identity() -> None:
    from pyfly.audit import AuditCheckpoint, AuditHead

    with pytest.raises(ValidationError):
        AuditCheckpoint(
            journal_id="default",
            scope=event().scope,
            head=AuditHead(chain_id="0" * 64, sequence=1, entry_hash="1" * 64),
            issued_at=datetime(2026, 10, 31, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("reason_codes", ["SECRET CANARY"]),
        ("event_type", "https://example.invalid"),
        ("event_id", "a" * 129),
        ("references", [{"kind": "x", "resource_id": "x"}] * 17),
    ],
)
def test_fact_bounds_prevent_content_and_unbounded_export(field: str, value: object) -> None:
    body = event().model_dump()
    body[field] = value
    with pytest.raises(ValidationError):
        AuditEvent.model_validate(body)


def test_nested_forged_models_are_revalidated_without_serialization_warnings() -> None:
    import warnings

    from pyfly.audit import AuditCorrelation

    forged = event().model_copy(update={"correlation": AuditCorrelation.model_construct(operation_id="SECRET CANARY")})
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(ValidationError) as failed:
            AuditEvent.model_validate(forged)
    assert not caught
    assert "SECRET CANARY" not in str(failed.value)


def test_domain_contract_imports_no_sql_driver() -> None:
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import pyfly.audit; assert 'sqlalchemy' not in sys.modules",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
