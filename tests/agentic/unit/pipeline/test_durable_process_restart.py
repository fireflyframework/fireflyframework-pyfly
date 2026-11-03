# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Two actual owned interpreters; completed effects cannot be replayed after a pause."""

import json
import os
import subprocess
import sys
from pathlib import Path

PROGRAM = r"""
import asyncio,json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[2])
from pyfly.agentic.pipeline import DAG,DAGNode,DAGEdge,PipelineEngine,Pause
from pyfly.agentic.pipeline.durable import BoundDurableCheckpointer,durable_graph_digest
from tests.agentic.unit.pipeline.test_durable_sql import opened,KEY,BINDING,INPUT,AUTHOR,REVIEWER
root=Path(sys.argv[1]);phase=sys.argv[3]
class Step:
    def __init__(self,name):self.name=name
    async def execute(self,context,inputs):
        if self.name=='wait':return Pause('approve')
        with (root/(self.name+'.effect')).open('x') as target:target.write(self.name)
        if self.name=='a':return {'answer':7}
        assert context.get_node_result('a').output=={'answer':7}
        assert context.inputs==INPUT
        return 'done'
def graph():
    dag=DAG('flow')
    for name in ('a','wait','b'):dag.add_node(DAGNode(node_id=name,step=Step(name)))
    dag.add_edge(DAGEdge(source='a',target='wait'));dag.add_edge(DAGEdge(source='wait',target='b'))
    return dag
binding=BINDING.model_copy(update={
    'graph_version':'pyfly-linear-v1','graph_digest':durable_graph_digest(graph())})
async def main():
    async with opened(root/'run.db',create=phase=='first') as (store,_,journal):
        if phase=='first':
            await store.create(KEY,binding,INPUT,actor=AUTHOR)
            lease=await store.claim(KEY,binding,owner='worker-one',expected_generation=1)
            engine=PipelineEngine(graph(),checkpointer=BoundDurableCheckpointer(store,lease))
            result=await engine.run(inputs=INPUT,run_id='run')
            assert result.paused and not (root/'b.effect').exists()
        else:
            value=await store.load(KEY,binding)
            assert value.status=='waiting_approval' and value.lease is None
            assert value.checkpoint.state['results']['a']['output']=={'answer':7}
            await store.decide(KEY,binding,task_id=value.task.task_id,expected_generation=1,decision='approve',
                               actor=REVIEWER,idempotency_key='review',policy_digest='c'*64)
            lease=await store.claim(KEY,binding,owner='worker-two',expected_generation=2)
            result=await PipelineEngine(graph(),checkpointer=BoundDurableCheckpointer(store,lease)).run(run_id='run')
            assert result.success
            await store.finish(lease,outcome='succeeded')
        value=await store.load(KEY,binding)
        assert (await journal.verify(KEY.scope)).head.sequence==value.version
        (root/(phase+'.json')).write_text(json.dumps({'pid':os.getpid(),'status':value.status,'version':value.version}))
import os
asyncio.run(main())
"""


def test_new_process_restores_pause_and_original_input_without_repeating_completed_effect(tmp_path):
    project = Path(__file__).resolve().parents[4]
    environment = {key: value for key, value in os.environ.items() if key in {"PATH", "HOME", "TMPDIR", "SYSTEMROOT"}}
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    for phase in ("first", "second"):
        with (tmp_path / (phase + ".log")).open("w") as log:
            completed = subprocess.run(
                [sys.executable, "-I", "-B", "-c", PROGRAM, str(tmp_path), str(project), phase],
                cwd=tmp_path,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=120,
                check=False,
            )
        assert completed.returncode == 0, (tmp_path / (phase + ".log")).read_text()[-4000:]
    first = json.loads((tmp_path / "first.json").read_text())
    second = json.loads((tmp_path / "second.json").read_text())
    assert first["pid"] != second["pid"]
    assert first["status"] == "waiting_approval" and second["status"] == "succeeded"
    assert second["version"] > first["version"]
    assert (tmp_path / "a.effect").read_text() == "a" and (tmp_path / "b.effect").read_text() == "b"
