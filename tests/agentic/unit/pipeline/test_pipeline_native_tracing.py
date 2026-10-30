"""Private native DI providers own pipeline/node spans and cancellation lifetime."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from pyfly.agentic import Agent, AgenticRuntime
from pyfly.agentic.auto_configuration import AgenticAutoConfiguration
from pyfly.agentic.pipeline.dag import DAG, DAGEdge, DAGNode
from pyfly.agentic.pipeline.engine import PipelineEngine, Send
from pyfly.context import ApplicationContext
from pyfly.core import Config
from pyfly.observability.auto_configuration import MeterProviderAutoConfiguration, TracingAutoConfiguration


@asynccontextmanager
async def native_tracing():
    global_provider = trace.get_tracer_provider()
    context = ApplicationContext(
        Config(
            {
                "pyfly": {
                    "agentic": {
                        "enabled": True,
                        "plugin-auto-discover": False,
                        "instrumentation-include-content": False,
                    },
                    "observability": {
                        "tracing": {"register-global": False, "exporter": "none"},
                        "metrics": {"register-global": False, "exporter": "none"},
                    },
                }
            }
        ),
        auto_configurations=[AgenticAutoConfiguration, TracingAutoConfiguration, MeterProviderAutoConfiguration],
    )
    await context.start()
    runtime = context.get_bean(AgenticRuntime)
    exporter = InMemorySpanExporter()
    stopped = []

    class ShutdownRecorder(SimpleSpanProcessor):
        def shutdown(self):
            stopped.append(time.time_ns())
            super().shutdown()

    runtime.tracer_provider.add_span_processor(ShutdownRecorder(exporter))
    try:
        with runtime.activate():
            yield runtime, exporter
    finally:
        await context.stop()
        assert trace.get_tracer_provider() is global_provider
        assert len(stopped) == 1
        assert all(span.end_time <= stopped[0] for span in exporter.get_finished_spans())


async def test_private_di_pipeline_and_node_spans_parent_actual_agent_spans():
    async with native_tracing() as (runtime, exporter):

        class ModelStep:
            async def execute(self, context, inputs):
                model = FunctionModel(lambda messages, info: ModelResponse(parts=[TextPart("done")]))
                return (
                    await Agent(
                        name="native", model=model, runtime=runtime, auto_register=False, default_middleware=False
                    ).run("private prompt")
                ).output

        dag = DAG("private")
        dag.add_node(DAGNode(node_id="one", step=ModelStep()))
        dag.add_node(DAGNode(node_id="two", step=ModelStep()))
        dag.add_edge(DAGEdge(source="one", target="two"))
        with runtime.tracer.custom_span("workload"):
            assert (await PipelineEngine(dag).run(inputs="input")).success
        spans = exporter.get_finished_spans()
        by_name = {span.name: span for span in spans}
        assert "pipeline.private" in by_name
        pipeline = by_name["pipeline.private"]
        assert pipeline.parent.span_id == by_name["workload"].context.span_id
        nodes = [by_name["pipeline.node." + node] for node in ("one", "two")]
        assert all(node.parent.span_id == pipeline.context.span_id for node in nodes)
        agents = [span for span in spans if span.attributes.get("gen_ai.operation.name") == "invoke_agent"]
        assert len(agents) == 2
        assert {span.parent.span_id for span in agents} == {node.context.span_id for node in nodes}


@pytest.mark.parametrize("cyclic", [False, True])
async def test_failed_node_spans_mark_type_only_without_exception_events(cyclic):
    class Failure:
        async def execute(self, context, inputs):
            raise ValueError("PRIVATE_EXCEPTION_SECRET")

    async with native_tracing() as (_, exporter):
        dag = DAG("failed")
        dag.add_node(DAGNode(node_id="bad", step=Failure()))
        result = await PipelineEngine(dag, routers={"bad": lambda state: None} if cyclic else None).run(inputs="input")
        assert not result.success
        spans = exporter.get_finished_spans()
        assert len(spans) == 2
        assert {span.status.status_code.name for span in spans} == {"ERROR"}
        assert {span.attributes.get("error.type") for span in spans} == {"ValueError", "PipelineError"}
        assert all(span.status.description is None and not span.events for span in spans)
        assert "PRIVATE_EXCEPTION_SECRET" not in str([(s.attributes, s.status, s.events) for s in spans])


@pytest.mark.parametrize("mode", ["topological", "cyclic", "fanout"])
async def test_cancel_drains_owned_nodes_before_pipeline_span_ends(mode):
    started, drained = set(), set()
    ready = asyncio.Event()
    expected = {"one"} if mode == "cyclic" else {"one", "two"}

    class Blocking:
        def __init__(self, name):
            self.name = name

        async def execute(self, context, inputs):
            started.add(self.name)
            if started == expected:
                ready.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0.01)
                drained.add(self.name)

    async with native_tracing() as (_, exporter):
        dag = DAG("cancelled")
        if mode == "fanout":

            class Dispatch:
                async def execute(self, context, inputs):
                    return [Send(name, {}) for name in sorted(expected)]

            dag.add_node(DAGNode(node_id="dispatch", step=Dispatch()))
        for name in expected:
            dag.add_node(DAGNode(node_id=name, step=Blocking(name)))
            if mode == "fanout":
                dag.add_edge(DAGEdge(source="dispatch", target=name))
        engine = PipelineEngine(dag, routers={"one": lambda state: None} if mode == "cyclic" else None)
        task = asyncio.create_task(engine.run(inputs="input"))
        await asyncio.wait_for(ready.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert drained == expected
        spans = exporter.get_finished_spans()
        pipeline = next(span for span in spans if span.name == "pipeline.cancelled")
        nodes = [span for span in spans if span.name in {"pipeline.node." + name for name in expected}]
        assert len(nodes) == len(expected)
        assert all(node.end_time <= pipeline.end_time for node in nodes)
        assert all(span.status.status_code.name == "ERROR" for span in [pipeline, *nodes])
        assert all(span.attributes.get("error.type") == "CancelledError" for span in [pipeline, *nodes])
        assert all(not span.events and span.status.description is None for span in spans)


async def test_cancelled_start_callback_still_closes_pipeline_span():
    class StopOnStart:
        async def on_pipeline_start(self, pipeline_name, run_id):
            raise asyncio.CancelledError("PRIVATE_CANCELLATION_MESSAGE")

    async with native_tracing() as (_, exporter):
        with pytest.raises(asyncio.CancelledError):
            await PipelineEngine(DAG("start"), event_handler=StopOnStart()).run(inputs="input")
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].status.status_code.name == "ERROR"
        assert spans[0].attributes["error.type"] == "CancelledError"
        assert not spans[0].events
        assert spans[0].status.description is None
