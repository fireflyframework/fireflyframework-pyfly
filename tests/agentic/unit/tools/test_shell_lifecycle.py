"""Real local subprocesses are reaped and their pipes drained before tool exit."""

import asyncio
import shlex
import sys
from contextlib import suppress

import pytest

from pyfly.agentic.exceptions import ToolError
from pyfly.agentic.tools.builtins.shell import ShellTool


@pytest.mark.parametrize("ending", ["timeout", "direct_timeout", "cancel", "cancel_again"])
async def test_shell_failure_waits_for_owned_child_and_pipe_cleanup(monkeypatch, ending):
    created = asyncio.Event()
    drained = asyncio.Event()
    release = asyncio.Event()
    processes = []
    original_spawn = asyncio.create_subprocess_exec

    async def spawn(*args, **kwargs):
        process = await original_spawn(*args, **kwargs)
        processes.append(process)
        communicate = process.communicate

        async def observed_communicate():
            result = await communicate()
            drained.set()
            if ending == "cancel_again":
                await release.wait()
            return result

        process.communicate = observed_communicate
        created.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    script = (
        "import sys,time; "
        "sys.stdout.write('fictional stdout\\n' * 10000); sys.stdout.flush(); "
        "sys.stderr.write('fictional stderr\\n' * 10000); sys.stderr.flush(); time.sleep(30)"
    )
    command = shlex.join([sys.executable, "-u", "-c", script])
    tool = ShellTool(allowed_commands=[sys.executable], timeout=0.05 if ending in ("timeout", "direct_timeout") else 10)
    operation = tool._execute if ending == "direct_timeout" else tool.execute
    task = asyncio.create_task(operation(command=command))
    try:
        await asyncio.wait_for(created.wait(), timeout=2)
        if ending in ("timeout", "direct_timeout"):
            expected = TimeoutError if ending == "direct_timeout" else ToolError
            with pytest.raises(expected, match="timed out after 0.05s"):
                await asyncio.wait_for(task, timeout=2)
        else:
            task.cancel()
            if ending == "cancel_again":
                await asyncio.wait_for(drained.wait(), timeout=2)
                assert not task.done()
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done()
                release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=2)
        process = processes[0]
        assert process.returncode is not None
        assert drained.is_set()
        assert process.stdout.at_eof()
        assert process.stderr.at_eof()
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for process in processes:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
            await asyncio.wait_for(process.communicate(), timeout=2)
