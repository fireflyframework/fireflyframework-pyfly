# Copyright 2026 Firefly Software Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Lab subpackage -- sessions, benchmarks, comparisons, datasets, and evaluation."""

from pyfly.agentic.lab._inputs import LabInput
from pyfly.agentic.lab.benchmark import Benchmark, BenchmarkResult
from pyfly.agentic.lab.comparison import ComparisonEntry, ModelComparison
from pyfly.agentic.lab.dataset import EvalCase, EvalDataset
from pyfly.agentic.lab.evaluator import EvalOrchestrator, EvalReport, EvalResult
from pyfly.agentic.lab.session import LabSession, SessionEntry

__all__ = [
    "Benchmark",
    "BenchmarkResult",
    "ComparisonEntry",
    "EvalCase",
    "EvalDataset",
    "EvalOrchestrator",
    "EvalReport",
    "EvalResult",
    "LabInput",
    "LabSession",
    "ModelComparison",
    "SessionEntry",
]
