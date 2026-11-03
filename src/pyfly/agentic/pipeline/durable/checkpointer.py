# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Claim-bound checkpoint policy for direct native and remote worker transports."""

from __future__ import annotations

import hashlib
from typing import Protocol, runtime_checkable

from pyfly.agentic.pipeline.checkpoint import CheckpointRecord
from pyfly.agentic.pipeline.dag import DAG
from pyfly.agentic.pipeline.durable.models import DurableBinding, DurableClaim, DurableStateError, bounded_json
from pyfly.agentic.pipeline.durable.ports import DurableRunStore

GRAPH_VERSION = "pyfly-linear-v1"


def durable_graph_digest(dag: DAG) -> str:
    """Digest the admitted native topology and execution options, excluding Python code.

    Host plan_digest must independently bind executors, models, tools and policy.
    Only a static line with no conditional dispatch or automatic retries qualifies.
    """
    try:
        if dag.is_cyclic() or not 3 <= len(dag.nodes) <= 64:
            raise ValueError
        nodes = dag.topological_sort()
        if len(dag.edges) != len(nodes) - 1 or {(e.source, e.target) for e in dag.edges} != set(
            zip(nodes, nodes[1:], strict=False)
        ):
            raise ValueError
        if any(n.condition is not None or n.retry_max != 0 for n in dag.nodes.values()):
            raise ValueError
        if any(e.condition is not None for e in dag.edges):
            raise ValueError
        manifest = {
            "version": GRAPH_VERSION,
            "pipeline_name": dag.name,
            "nodes": [
                {
                    "id": n,
                    "failure_strategy": dag.nodes[n].failure_strategy.value,
                    "timeout_seconds": float(dag.nodes[n].timeout_seconds) or 0.0,
                }
                for n in nodes
            ],
            "edges": [
                {"source": e.source, "target": e.target, "output_key": e.output_key, "input_key": e.input_key}
                for e in sorted(dag.edges, key=lambda e: (e.source, e.target))
            ],
        }
        return hashlib.sha256(bounded_json(manifest).encode()).hexdigest()
    except (ValueError, TypeError, AttributeError):
        raise DurableStateError("Durable execution requires a static linear graph without retries") from None


@runtime_checkable
class DurableCheckpointer(Protocol):
    """RPC implementations must enforce these checks on the authoritative server."""

    binding: DurableBinding

    async def save(self, record: CheckpointRecord) -> None: ...
    async def load_latest(self, pipeline_name: str, run_id: str) -> CheckpointRecord | None: ...
    async def validate_run(self, pipeline_name: str, run_id: str | None, inputs: object, *, resume: bool) -> None: ...
    async def before_node(self, node_id: str) -> None: ...


class BoundDurableCheckpointer:
    def __init__(self, store: DurableRunStore, claim: DurableClaim) -> None:
        self.store = store
        try:
            if not isinstance(claim, DurableClaim):
                raise ValueError
            self.claim = DurableClaim.model_validate(claim)
        except ValueError:
            raise DurableStateError("Durable claim reference is invalid") from None
        self.binding = self.claim.binding

    async def validate_run(self, pipeline_name: str, run_id: str | None, inputs: object, *, resume: bool) -> None:
        if pipeline_name != self.binding.pipeline_name or run_id != self.claim.key.run_id:
            raise DurableStateError("Durable engine identity differs from the admitted claim")
        envelope = await self.store.validate_claim(self.claim)
        if resume:
            if await self.store.checkpoint_for(self.claim) is None:
                raise DurableStateError("Durable resume requires a persisted checkpoint")
        elif bounded_json(inputs) != bounded_json(envelope.original_input) or envelope.checkpoint is not None:
            raise DurableStateError("Durable initial input or continuation differs")

    async def before_node(self, node_id: str) -> None:
        await self.store.validate_claim(self.claim, node_id=node_id)

    async def save(self, record: CheckpointRecord) -> None:
        await self.store.checkpoint(self.claim, record)

    async def load_latest(self, pipeline_name: str, run_id: str) -> CheckpointRecord | None:
        if pipeline_name != self.binding.pipeline_name or run_id != self.claim.key.run_id:
            raise DurableStateError("Durable checkpoint identity differs")
        return await self.store.checkpoint_for(self.claim)
