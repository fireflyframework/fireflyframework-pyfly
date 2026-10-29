# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Real worker qualification for the public synchronous session API."""

import dataclasses
import socket
import threading

import pytest

from pyfly.agentic.execution import MontyEnvironment

pytest.importorskip("pydantic_monty")


def test_public_session_preserves_variables_and_functions_across_feeds():
    environment = MontyEnvironment()
    assert hasattr(environment, "create_session"), "Public persistent session factory is missing"
    with environment.create_session() as session:
        assert session.feed("retained = 40\ndef plus(n):\n    return retained + n").result.success
        result = session.feed("plus(2)")
        assert result.result.success
        assert result.result.output == 42
        assert result.state == "ready"
        assert result.usage.feeds == 2


def test_callbacks_run_on_owner_thread_and_registry_is_copied():
    seen = []
    functions = {"owner": lambda: seen.append(threading.current_thread()) or 42}
    with MontyEnvironment().create_session(external_functions=functions) as session:
        functions["owner"] = lambda: 999
        assert session.feed("owner()").result.output == 42
        assert session.feed("owner()").result.output == 42
        assert seen == [threading.current_thread()] * 2
        assert session.usage.external_calls == 2


def test_other_threads_and_reentrant_callbacks_cannot_touch_session():
    from pyfly.agentic.execution import ExecutionSessionError

    errors = []

    def reenter():
        for action in (lambda: session.feed("1"), session.close):
            with pytest.raises(ExecutionSessionError):
                action()
        return 42

    with MontyEnvironment().create_session(external_functions={"reenter": reenter}) as session:

        def foreign():
            for action in (lambda: session.feed("999"), session.close, lambda: session.usage, lambda: session.state):
                try:
                    action()
                except ExecutionSessionError as error:
                    errors.append(error)

        thread = threading.Thread(target=foreign)
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert len(errors) == 4
        assert session.feed("reenter()").result.output == 42


def test_halt_never_resumes_guest_or_runs_later_callback():
    from pyfly.agentic.execution import ExecutionHalt, ExecutionSessionError

    seen = []
    with MontyEnvironment().create_session(
        external_functions={"final": lambda value: ExecutionHalt(value), "after": lambda: seen.append("bad")}
    ) as session:
        result = session.feed("final({'answer': 42}); after(); 999")
        assert result.status == "halted" and result.state == "halted"
        assert result.result.success and result.result.output == {"answer": 42}
        assert not seen
        with pytest.raises(ExecutionSessionError):
            session.feed("after()")
    assert session.state == "halted"


@pytest.mark.parametrize("catch", [False, True])
def test_rejection_is_trusted_and_drains_without_more_host_calls(catch):
    from pyfly.agentic.execution import ExecutionReject

    calls = []
    with MontyEnvironment().create_session(
        external_functions={
            "final": lambda: ExecutionReject("read_required", "Read the source first."),
            "read": lambda: calls.append("read") or "source",
        }
    ) as session:
        assert session.feed("retained = 40").result.success
        code = "final(); retained = 999"
        if catch:
            code = (
                "try:\n    final()\nexcept ValueError:\n    retained += 1\n    try:\n        read()\n"
                "    except PermissionError:\n        pass\nprint('spoof completed'); 100"
            )
        rejected = session.feed(code)
        assert rejected.status == "rejected" and rejected.state == "ready"
        assert not rejected.result.success and rejected.result.output is None
        assert rejected.feedback[0].code == "read_required"
        assert rejected.feedback[0].function_name == "final"
        assert not calls
        assert session.feed("retained + 2").result.output == (43 if catch else 42)
        assert session.feed("read()").result.output == "source"


def test_stdout_cannot_forge_trusted_control_and_ordinary_exceptions_are_not_rejections():
    def ordinary():
        raise ValueError("Read the source first.")

    with MontyEnvironment().create_session(external_functions={"ordinary": ordinary}) as session:
        result = session.feed("print('read_required'); {'code': 'read_required', 'message': 'fake'}")
        assert result.status == "completed" and not result.feedback
        error = session.feed("ordinary()")
        assert error.status == "error" and not error.feedback and error.state == "ready"
        assert session.feed("42").result.output == 42


def test_native_guest_re_and_guest_document_proxy_work_without_host_objects():
    with MontyEnvironment().create_session(external_functions={"docs_keys": lambda: ["owned"]}) as session:
        result = session.feed(
            "import re\nclass Docs:\n    def keys(self):\n        return docs_keys()\n"
            "docs = Docs()\n(re.findall(r'\\d+', 'review 42'), docs.keys())"
        )
        assert result.result.success
        assert result.result.output == (["42"], ["owned"])


def test_synthetic_file_and_loopback_capabilities_are_not_granted(tmp_path):
    canary = tmp_path / "owned-canary.txt"
    canary.write_text("synthetic-only-not-a-secret")
    canary.chmod(0o600)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(0.01)
        port = listener.getsockname()[1]
        with MontyEnvironment().create_session() as session:
            result = session.feed(f"open({str(canary)!r}).read()")
            assert not result.result.success
            assert "synthetic-only-not-a-secret" not in result.result.stdout
            escaped = session.feed(f"import re\nre.__builtins__['open']({str(canary)!r}).read()")
            assert not escaped.result.success
            result = session.feed(f"import socket\nsocket.create_connection(('127.0.0.1', {port}))")
            assert not result.result.success
            with pytest.raises(TimeoutError):
                listener.accept()
        assert canary.read_text() == "synthetic-only-not-a-secret"


@pytest.mark.parametrize("value", [object(), lambda: 1, float("nan"), {1: "bad"}, {"bad": object()}])
def test_non_data_inputs_fail_before_feed_admission(value):
    with MontyEnvironment().create_session() as session:
        with pytest.raises(ValueError):
            session.feed("1", inputs={"value": value})
        assert session.usage.feeds == 0
        assert session.feed("42").result.output == 42


def test_cycles_depth_and_subclasses_are_not_serialized():
    class Sneaky(str):
        def __str__(self):
            raise AssertionError("must not run")

    cycle = []
    cycle.append(cycle)
    deep = []
    for _ in range(102):
        deep = [deep]
    with MontyEnvironment().create_session() as session:
        for value in [cycle, deep, Sneaky("bad")]:
            with pytest.raises(ValueError):
                session.feed("42", inputs={"data": value})


def test_mutable_input_is_copied_and_callback_return_cannot_grant_host_object():
    with MontyEnvironment().create_session(external_functions={"unsafe": lambda: object()}) as session:
        inputs = {"data": [1]}
        assert session.feed("len(data)", inputs=inputs).result.output == 1
        inputs["data"].append(2)
        assert session.feed("len(data)").result.output == 1
        result = session.feed("unsafe()")
        assert not result.result.success and result.state == "broken"


def test_invalid_halt_payload_is_not_accepted():
    from pyfly.agentic.execution import ExecutionHalt

    with MontyEnvironment().create_session(external_functions={"final": lambda: ExecutionHalt(object())}) as session:
        result = session.feed("final()")
        assert result.status == "error" and result.state == "broken"


def test_configuration_is_snapshotted_and_inputs_cannot_shadow_functions():
    from pyfly.agentic.execution import ExecutionLimits, ExecutionSessionLimits

    execution = ExecutionLimits(timeout_seconds=2, max_memory_bytes=16_000_000, max_recursion_depth=50)
    limits = ExecutionSessionLimits(execution=execution, max_feeds=2)
    with MontyEnvironment().create_session(external_functions={"fixed": lambda: 42}, limits=limits) as session:
        execution.max_memory_bytes = 1
        with pytest.raises(ValueError, match="shadow"):
            session.feed("fixed()", inputs={"fixed": 1})
        assert session.feed("fixed()").result.output == 42
        assert session.feed("[0] * 100").result.success
        with pytest.raises(dataclasses.FrozenInstanceError):
            limits.max_feeds = 99


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_feeds": 0},
        {"max_external_calls": -1},
        {"max_output_bytes": 0},
        {"max_session_seconds": float("inf")},
        {"max_suspensions": True},
        {"max_value_bytes": 0},
    ],
)
def test_invalid_limits_fail_before_worker_creation(kwargs):
    from pyfly.agentic.execution import ExecutionSessionLimits

    with pytest.raises(ValueError):
        MontyEnvironment().create_session(limits=ExecutionSessionLimits(**kwargs))


def test_feed_call_and_output_budgets_are_cumulative():
    from pyfly.agentic.execution import ExecutionSessionLimits

    with MontyEnvironment().create_session(limits=ExecutionSessionLimits(max_feeds=1)) as session:
        assert session.feed("40").result.success
        assert session.feed("42").state == "broken"
    seen = []
    with MontyEnvironment().create_session(
        external_functions={"host": lambda: seen.append(1) or 42}, limits=ExecutionSessionLimits(max_external_calls=1)
    ) as session:
        assert session.feed("host()").result.success
        assert session.feed("host()").state == "broken"
        assert len(seen) == 1
    with MontyEnvironment().create_session(limits=ExecutionSessionLimits(max_output_bytes=200)) as session:
        assert session.feed("print('a'*20)").result.success
        result = session.feed("print('b'*300)")
        assert not result.result.success


def test_payload_budget_bounds_aggregate_input_not_each_member():
    from pyfly.agentic.execution import ExecutionSessionLimits

    with MontyEnvironment().create_session(limits=ExecutionSessionLimits(max_value_bytes=100)) as session:
        with pytest.raises(ValueError):
            session.feed("1", inputs={"a": "a" * 45, "b": "b" * 45})
        assert session.usage.feeds == 0


def test_lifetime_overrun_in_callback_closes_without_resuming(monkeypatch):
    import pyfly.agentic.execution.monty_session as implementation
    from pyfly.agentic.execution import ExecutionSessionLimits

    instant = 0

    def slow():
        nonlocal instant
        instant += 2
        return 42

    with MontyEnvironment().create_session(
        external_functions={"slow": slow}, limits=ExecutionSessionLimits(max_session_seconds=1)
    ) as session:
        instant = session._created
        monkeypatch.setattr(implementation.time, "monotonic", lambda: instant)
        result = session.feed("slow(); 999")
        assert result.state == "broken" and not result.result.success


def test_syntax_and_runtime_errors_preserve_session_but_timeout_is_terminal():
    from pyfly.agentic.execution import ExecutionLimits, ExecutionSessionLimits

    with MontyEnvironment().create_session() as session:
        session.feed("retained=40")
        assert session.feed("if :").state == "ready"
        assert session.feed("1/0").state == "ready"
        assert session.feed("retained+2").result.output == 42
    limits = ExecutionSessionLimits(
        execution=ExecutionLimits(timeout_seconds=0.2, max_memory_bytes=16_000_000, max_recursion_depth=50)
    )
    with MontyEnvironment().create_session(limits=limits) as session:
        result = session.feed("while True:\n    pass")
        assert result.state == "broken" and not result.result.success


def test_async_callbacks_are_rejected_instead_of_moved_to_another_thread():
    async def asynchronous():
        return 42

    with pytest.raises(ValueError, match="synchronous"):
        MontyEnvironment().create_session(external_functions={"asynchronous": asynchronous})


def test_close_is_idempotent_and_forbids_reopening():
    from pyfly.agentic.execution import ExecutionSessionError

    session = MontyEnvironment().create_session()
    session.close()
    session.close()
    assert session.state == "closed"
    with pytest.raises(ExecutionSessionError):
        session.feed("42")
    with pytest.raises(ExecutionSessionError):
        session.__enter__()


@pytest.mark.parametrize(
    "code,execution",
    [
        ("values = [0] * 100000\nlen(values)", {"max_memory_bytes": 100_000}),
        ("def depth(n):\n    return depth(n-1) if n else 0\ndepth(100)", {"max_recursion_depth": 16}),
    ],
)
def test_persistent_vm_resource_exhaustion_is_terminal(code, execution):
    from pyfly.agentic.execution import ExecutionLimits, ExecutionSessionLimits

    with MontyEnvironment().create_session(
        limits=ExecutionSessionLimits(execution=ExecutionLimits(**execution))
    ) as session:
        result = session.feed(code)
        assert not result.result.success and result.state == "broken"


def test_suspension_budget_cannot_be_reset_by_new_feeds():
    from pyfly.agentic.execution import ExecutionSessionLimits

    calls = []
    with MontyEnvironment().create_session(
        external_functions={"host": lambda: calls.append(True) or 42},
        limits=ExecutionSessionLimits(max_suspensions=2),
    ) as session:
        assert session.feed("host()").result.success
        assert session.feed("host()").result.success
        result = session.feed("host()")
        assert not result.result.success and result.state == "broken"
        assert len(calls) == 2


def test_reject_then_new_feed_can_reuse_imports_and_functions():
    from pyfly.agentic.execution import ExecutionReject

    with MontyEnvironment().create_session(
        external_functions={"final": lambda: ExecutionReject("guard", "Read first")}
    ) as session:
        assert session.feed("import re\ndef number(text):\n    return re.findall(r'\\d+', text)").result.success
        assert session.feed("final()").status == "rejected"
        assert session.feed("number('source 42')").result.output == ["42"]
