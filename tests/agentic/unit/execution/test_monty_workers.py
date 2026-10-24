"""Real worker-process contracts for the native Monty adapter."""

from __future__ import annotations

import asyncio
import contextvars
import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

pytest.importorskip("pydantic_monty")

from pyfly.agentic.exceptions import SandboxUnavailableError  # noqa: E402
from pyfly.agentic.execution import ExecutionLimits, MontyEnvironment  # noqa: E402


def _workers() -> set[int]:
    if shutil.which("ps") is None:
        pytest.skip("worker process inventory requires ps")
    rows = subprocess.check_output(["ps", "-ww", "-axo", "pid=,ppid=,command="], text=True)
    found = set()
    for row in rows.splitlines():
        fields = row.strip().split(None, 2)
        if (
            len(fields) == 3
            and int(fields[1]) == os.getpid()
            and Path(fields[2].split()[0]).name in ("monty", "monty.exe")
        ):
            found.add(int(fields[0]))
    return found


async def test_async_tools_run_on_owner_loop_with_context_without_guest_await():
    owner = asyncio.get_running_loop()
    tenant = contextvars.ContextVar("tenant", default="absent")
    tenant.set("fictional-tenant")

    async def lookup(value):
        assert asyncio.get_running_loop() is owner
        assert tenant.get() == "fictional-tenant"
        return {"answer": value * 2}

    result = await MontyEnvironment().run_code(
        "lookup(n)['answer']", inputs={"n": 21}, external_functions={"lookup": lookup}
    )
    assert result.success and result.output == 42


async def test_sync_tools_are_off_loop_and_awaitable_results_are_awaited():
    owner_thread = threading.get_ident()

    def lookup(value):
        assert threading.get_ident() != owner_thread

        async def answer():
            assert threading.get_ident() == owner_thread
            return value * 2

        return answer()

    result = await MontyEnvironment().run_code("lookup(21)", external_functions={"lookup": lookup})
    assert result.success and result.output == 42


async def test_calls_get_fresh_state_and_unknown_tools_remain_unavailable():
    env = MontyEnvironment()
    first = await env.run_code("previous = 41\nprevious + 1")
    assert first.success and first.output == 42
    second = await env.run_code("previous")
    assert not second.success and "NameError" in second.error
    third = await env.run_code("not_registered()")
    assert not third.success and "NameError" in third.error


async def test_stdout_before_guest_or_host_error_is_retained():
    def fail():
        raise ValueError("fictional tool failure")

    result = await MontyEnvironment().run_code("print('before failure')\nfail()", external_functions={"fail": fail})
    assert not result.success and result.stdout == "before failure\n"
    assert "fictional tool failure" in result.error


async def test_syntax_error_is_a_result_and_workers_close():
    before = _workers()
    result = await MontyEnvironment().run_code("def (:")
    assert not result.success and result.error_type == "MontySyntaxError"
    assert _workers() == before


async def test_cancellation_cancels_host_coroutine_and_reaps_worker():
    before = _workers()
    entered, cleaned = asyncio.Event(), asyncio.Event()

    async def wait_here():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    env = MontyEnvironment()
    task = asyncio.create_task(env.run_code("wait_here()", external_functions={"wait_here": wait_here}))
    await asyncio.wait_for(entered.wait(), timeout=5)
    assert _workers() - before
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set() and _workers() == before
    following = await env.run_code("21 * 2")
    assert following.success and following.output == 42


async def test_wall_clock_budget_includes_awaited_host_work():
    before = _workers()
    cleaned = asyncio.Event()

    async def wait_here():
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    result = await asyncio.wait_for(
        MontyEnvironment().run_code(
            "print('starting')\nwait_here()",
            external_functions={"wait_here": wait_here},
            limits=ExecutionLimits(timeout_seconds=0.2),
        ),
        timeout=5,
    )
    assert not result.success and result.error_type == "TimeoutError"
    assert result.stdout == "starting\n" and cleaned.is_set()
    assert _workers() == before


async def test_concurrent_calls_have_independent_workers_and_callbacks():
    arrived = 0
    ready = asyncio.Event()

    async def barrier(value):
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            ready.set()
        await asyncio.wait_for(ready.wait(), timeout=5)
        return value

    env = MontyEnvironment()
    results = await asyncio.gather(
        env.run_code("barrier(n)", inputs={"n": 11}, external_functions={"barrier": barrier}),
        env.run_code("barrier(n)", inputs={"n": 29}, external_functions={"barrier": barrier}),
    )
    assert all(result.success for result in results)
    assert [result.output for result in results] == [11, 29]


async def test_allocation_limit_is_explicitly_rejected_without_starting_worker():
    before = _workers()
    with pytest.raises(ValueError, match="max_allocations.*not supported"):
        await MontyEnvironment().run_code("1 + 1", limits=ExecutionLimits(max_allocations=100))
    assert _workers() == before


@pytest.mark.parametrize(
    "code,limits",
    [
        ("values = [0] * 100000\nlen(values)", ExecutionLimits(max_memory_bytes=100_000)),
        ("def depth(n):\n    return depth(n - 1) if n else 0\ndepth(100)", ExecutionLimits(max_recursion_depth=16)),
    ],
)
async def test_native_memory_and_recursion_budgets_are_enforced(code, limits):
    result = await MontyEnvironment().run_code(code, limits=limits)
    assert not result.success and result.error


async def test_type_check_accepts_declared_inputs_tools_and_rejects_invalid_code():
    env = MontyEnvironment(type_check=True)
    result = await env.run_code("twice(n)", inputs={"n": 21}, external_functions={"twice": lambda n: n * 2})
    assert result.success and result.output == 42
    invalid = await env.run_code("number: int = 'not an integer'\nnumber")
    assert not invalid.success and invalid.error_type == "MontyTypingError"


@pytest.mark.parametrize("name", ["not-valid", "class", "invalid name"])
async def test_invalid_binding_names_are_rejected_before_type_stubs_or_workers(name):
    with pytest.raises(ValueError, match="identifier"):
        await MontyEnvironment(type_check=True).run_code("1 + 1", inputs={name: 1})


async def test_missing_runtime_binary_has_actionable_unavailable_error(monkeypatch, tmp_path):
    monkeypatch.setenv("MONTY_BIN", str(tmp_path / "missing-monty-binary"))
    with pytest.raises(SandboxUnavailableError, match="agentic-script-execution"):
        await MontyEnvironment().run_code("1 + 1")


async def test_timeout_during_worker_startup_is_not_reported_as_missing_runtime():
    before = _workers()
    result = await MontyEnvironment().run_code("1 + 1", limits=ExecutionLimits(timeout_seconds=0.000001))
    assert not result.success and result.error_type == "TimeoutError"
    assert _workers() == before


@pytest.mark.parametrize("name", ["_private", "__all__"])
async def test_type_checked_underscore_input_is_exported_to_the_guest(name):
    result = await MontyEnvironment(type_check=True).run_code(f"{name} + 1", inputs={name: 41})
    assert result.success and result.output == 42


@pytest.mark.parametrize("name", ["_double", "__all__"])
async def test_type_checked_underscore_tool_is_exported_to_the_guest(name):
    result = await MontyEnvironment(type_check=True).run_code(f"{name}(21)", external_functions={name: lambda n: n * 2})
    assert result.success and result.output == 42
