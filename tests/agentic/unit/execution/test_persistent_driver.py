# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Driver failures that require deliberately suspended or broken SDK resources."""

import os
import types
from dataclasses import dataclass

import pytest

from pyfly.agentic.execution import ExecutionHalt, ExecutionSessionLimits, MontyEnvironment

monty = pytest.importorskip("pydantic_monty")


def test_accepted_halt_cleanup_failure_retains_terminal_acceptance(monkeypatch):
    after = []
    with MontyEnvironment().create_session(
        external_functions={
            "final": lambda: ExecutionHalt(42),
            "after": lambda: after.append(True),
        }
    ) as session:
        real_close = session._stack.close

        def failed_close():
            real_close()
            raise RuntimeError("synthetic cleanup failure after owned-worker cleanup")

        monkeypatch.setattr(session._stack, "close", failed_close)
        result = session.feed("final(); after()")
        assert result.status == "halted" and result.state == "broken"
        assert not result.result.success and result.result.output == 42
        assert result.result.error_type == "ExecutionCleanupError"
        assert not after


def test_halt_close_and_timeout_reap_only_the_owned_worker():
    with MontyEnvironment().create_session(external_functions={"final": lambda: ExecutionHalt(42)}) as session:
        pid = session._session.worker_pid
        assert pid is not None
        assert session.feed("final()").result.success
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.parametrize("case", ["os", "object_call", "object_lookup", "unknown_call", "unknown_lookup", "future"])
def test_denied_snapshot_forms_never_dispatch_host_callbacks(monkeypatch, case):
    calls = []
    events = []

    @dataclass
    class Complete:
        output: object = 42

    class Function:
        function_name = "allowed" if case != "unknown_call" else "unknown"
        object_id = object() if case == "object_call" else None
        is_os_function = case == "os"
        args = ()
        kwargs = {}

        def resume(self, answer):
            events.append(answer)
            return Complete()

        def resume_not_handled(self):
            events.append("unhandled")
            return Complete()

    class Lookup:
        object_id = object() if case == "object_lookup" else None
        variable_name = "allowed" if case == "object_lookup" else "unknown"

        def resume(self):
            events.append("denied lookup")
            return Complete()

        def resume_auto(self):
            raise AssertionError("host object/unknown lookup must never auto-dispatch")

    with MontyEnvironment().create_session(external_functions={"allowed": lambda: calls.append(True)}) as session:
        monkeypatch.setattr(monty, "FunctionSnapshot", Function)
        monkeypatch.setattr(monty, "NameLookupSnapshot", Lookup)
        monkeypatch.setattr(monty, "MontyComplete", Complete)
        snapshot = (
            Lookup() if case in ("object_lookup", "unknown_lookup") else object() if case == "future" else Function()
        )
        monkeypatch.setattr(session, "_session", types.SimpleNamespace(feed_start=lambda *a, **kw: snapshot))
        result = session.feed("synthetic SDK snapshot")
        assert not calls
        if case == "future":
            assert result.state == "broken" and not result.result.success
        else:
            assert result.result.success
            assert events
            if case in ("object_call", "unknown_call"):
                assert isinstance(events[0]["exception"], AttributeError if case == "object_call" else NameError)


def test_whole_callback_arguments_and_result_have_independent_payload_checks():
    with MontyEnvironment().create_session(
        external_functions={"echo": lambda *values: list(values)},
        limits=ExecutionSessionLimits(max_value_bytes=100),
    ) as session:
        result = session.feed("echo('a' * 45, 'b' * 45)")
        assert result.state == "broken" and not result.result.success


def test_reject_drain_runaway_closes_and_preserves_trusted_feedback():
    from pyfly.agentic.execution import ExecutionLimits, ExecutionReject

    with MontyEnvironment().create_session(
        external_functions={"final": lambda: ExecutionReject("read_required", "Read first")},
        limits=ExecutionSessionLimits(execution=ExecutionLimits(timeout_seconds=0.2)),
    ) as session:
        result = session.feed("try:\n    final()\nexcept ValueError:\n    while True:\n        pass")
        assert result.state == "broken" and result.status == "error"
        assert result.feedback[0].code == "read_required"


def test_callable_async_objects_and_late_awaitables_are_not_executed():
    class AsyncObject:
        async def __call__(self):
            return 42

    with pytest.raises(ValueError, match="synchronous"):
        MontyEnvironment().create_session(external_functions={"host": AsyncObject()})

    async def answer():
        return 42

    with MontyEnvironment().create_session(external_functions={"host": lambda: answer()}) as session:
        result = session.feed("host()")
        assert result.state == "broken" and not result.result.success


def test_huge_numeric_limits_have_a_clear_validation_error():
    with pytest.raises(ValueError):
        MontyEnvironment().create_session(limits=ExecutionSessionLimits(max_session_seconds=10**1000))


def test_static_checking_is_explicitly_unavailable_before_worker_creation(monkeypatch):
    def forbidden_worker(**kwargs):
        raise AssertionError("static-context rejection must precede worker creation")

    monkeypatch.setattr(monty, "Monty", forbidden_worker)
    with pytest.raises(ValueError, match="0.0.23 loses static context"):
        MontyEnvironment(type_check=True).create_session()


def test_lifetime_is_checked_when_worker_returns_a_guest_error(monkeypatch):
    import pyfly.agentic.execution.monty_session as implementation

    with MontyEnvironment().create_session(limits=ExecutionSessionLimits(max_session_seconds=1)) as session:
        instant = session._created
        real_worker = session._session

        def failing_feed(*args, **kwargs):
            nonlocal instant
            instant += 2
            return real_worker.feed_start(*args, **kwargs)

        monkeypatch.setattr(implementation.time, "monotonic", lambda: instant)
        monkeypatch.setattr(session, "_session", types.SimpleNamespace(feed_start=failing_feed))
        result = session.feed("1/0")
        assert not result.result.success and result.state == "broken"


def test_keyboard_interrupt_from_host_closes_without_guest_resume():
    def interrupt():
        raise KeyboardInterrupt("owned synthetic interruption")

    with MontyEnvironment().create_session(external_functions={"interrupt": interrupt}) as session:
        pid = session._session.worker_pid
        with pytest.raises(KeyboardInterrupt, match="owned synthetic"):
            session.feed("interrupt(); 999")
        assert session.state == "broken"
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_oversized_guest_output_has_error_status_not_completed():
    with MontyEnvironment().create_session(limits=ExecutionSessionLimits(max_value_bytes=100)) as session:
        result = session.feed("'a' * 101")
        assert result.state == "broken" and result.status == "error"


@pytest.mark.parametrize("control", ["halt", "reject"])
def test_bounded_host_control_survives_callback_deadline_overrun(control, monkeypatch):
    import pyfly.agentic.execution.monty_session as implementation
    from pyfly.agentic.execution import ExecutionReject

    instant = 0

    def slow():
        nonlocal instant
        instant += 2
        return ExecutionHalt(42) if control == "halt" else ExecutionReject("read_required", "Read first")

    with MontyEnvironment().create_session(
        external_functions={"slow": slow},
        limits=ExecutionSessionLimits(max_session_seconds=1),
    ) as session:
        instant = session._created
        monkeypatch.setattr(implementation.time, "monotonic", lambda: instant)
        result = session.feed("slow(); 999")
        assert result.state == "broken" and not result.result.success
        if control == "halt":
            assert result.status == "halted" and result.result.output == 42
        else:
            assert result.status == "error" and result.feedback[0].code == "read_required"


@pytest.mark.parametrize("argument", [object(), "x" * 1000])
def test_ordinary_host_exception_payloads_are_bounded_data_only(argument):
    def failure():
        raise ValueError(argument)

    with MontyEnvironment().create_session(
        external_functions={"failure": failure},
        limits=ExecutionSessionLimits(max_value_bytes=100),
    ) as session:
        result = session.feed("try:\n    failure()\nexcept ValueError:\n    pass\n42")
        assert not result.result.success and result.state == "broken"


def test_application_exception_does_not_run_custom_string_conversion():
    class ApplicationError(Exception):
        def __str__(self):
            raise AssertionError("custom host exception string conversion must not run")

    def failure():
        raise ApplicationError("bounded application message")

    with MontyEnvironment().create_session(external_functions={"failure": failure}) as session:
        result = session.feed("try:\n    failure()\nexcept RuntimeError:\n    answer = 42\nanswer")
        assert result.result.success and result.result.output == 42
        assert not result.feedback


@pytest.mark.parametrize("field", ["max_output_bytes", "max_suspensions", "max_memory_bytes", "timeout_seconds"])
def test_native_unsafe_finite_limits_are_rejected_before_pool_creation(monkeypatch, field):
    from pyfly.agentic.execution import ExecutionLimits

    def forbidden_worker(**kwargs):
        raise AssertionError("range validation must precede worker creation")

    monkeypatch.setattr(monty, "Monty", forbidden_worker)
    if field in ("max_memory_bytes", "timeout_seconds"):
        limits = ExecutionSessionLimits(execution=ExecutionLimits(**{field: 10**100}))
    else:
        limits = ExecutionSessionLimits(**{field: 10**100})
    with pytest.raises(ValueError):
        MontyEnvironment().create_session(limits=limits)


@pytest.mark.parametrize("boundary", ["arguments", "value", "exception"])
def test_copy_work_cannot_admit_callback_or_guest_resume_after_expiry(monkeypatch, boundary):
    import pyfly.agentic.execution.monty_session as implementation

    calls = []
    instant = 0

    def host():
        calls.append(True)
        if boundary == "exception":
            raise ValueError("safe")
        return "safe"

    with MontyEnvironment().create_session(
        external_functions={"host": host},
        limits=ExecutionSessionLimits(max_session_seconds=1),
    ) as session:
        instant = session._created
        monkeypatch.setattr(implementation.time, "monotonic", lambda: instant)
        original_copy = session._copy

        def slow_copy(value):
            nonlocal instant
            if (
                (boundary == "arguments" and value == ((), {}))
                or (boundary == "value" and value == "safe")
                or (boundary == "exception" and value == ("safe",))
            ):
                instant += 2
            return original_copy(value)

        monkeypatch.setattr(session, "_copy", slow_copy)
        result = session.feed("try:\n    host()\nexcept ValueError:\n    pass\nprint('must-not-print')")
        assert result.state == "broken" and not result.result.success
        assert len(calls) == (0 if boundary == "arguments" else 1)
        assert "must-not-print" not in result.result.stdout
