# Copyright 2026 Firefly Software Foundation.
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
"""Thread dump actuator endpoint — Spring Boot ``/actuator/threaddump`` parity."""

from __future__ import annotations

import sys
import threading
import traceback
from typing import Any


class ThreadDumpEndpoint:
    """Exposes a snapshot of all live threads at ``/actuator/threaddump``."""

    @property
    def endpoint_id(self) -> str:
        return "threaddump"

    @property
    def enabled(self) -> bool:
        return True

    async def handle(self, context: Any = None) -> dict[str, Any]:
        frames = sys._current_frames()
        threads_by_id = {t.ident: t for t in threading.enumerate()}

        threads: list[dict[str, Any]] = []
        for thread_id, frame in frames.items():
            thread = threads_by_id.get(thread_id)
            stack = [
                {
                    "className": fs.name,
                    "methodName": fs.name,
                    "fileName": fs.filename,
                    "lineNumber": fs.lineno,
                }
                for fs in traceback.extract_stack(frame)
            ]
            threads.append(
                {
                    "threadName": thread.name if thread else f"Thread-{thread_id}",
                    "threadId": thread_id,
                    "daemon": bool(thread.daemon) if thread else False,
                    "threadState": "RUNNABLE",
                    "stackTrace": stack,
                }
            )

        return {"threads": threads}
