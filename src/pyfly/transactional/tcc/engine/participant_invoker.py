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
"""TCC participant invoker — invokes try/confirm/cancel methods with resolved arguments."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from pyfly.transactional.tcc.core.context import TccContext
from pyfly.transactional.tcc.engine.argument_resolver import TccArgumentResolver
from pyfly.transactional.tcc.registry.participant_definition import (
    ParticipantDefinition,
)


class TccParticipantInvoker:
    """Invokes TCC participant methods (try/confirm/cancel) with resolved arguments."""

    def __init__(self, argument_resolver: TccArgumentResolver) -> None:
        self._argument_resolver = argument_resolver

    async def invoke_try(
        self,
        participant_def: ParticipantDefinition,
        bean: Any,
        ctx: TccContext,
        input_data: Any = None,
    ) -> Any:
        """Invoke the try method with resolved arguments.

        Returns the try result.

        Raises:
            ValueError: If the participant has no try_method defined.
        """
        method = participant_def.try_method
        if method is None:
            raise ValueError(f"Participant '{participant_def.id}' has no try_method defined.")

        kwargs = self._argument_resolver.resolve(
            method,
            bean,
            ctx,
            input_data=input_data,
            participant_id=participant_def.id,
        )
        return await self._call(method, bean, kwargs)

    async def invoke_confirm(
        self,
        participant_def: ParticipantDefinition,
        bean: Any,
        ctx: TccContext,
    ) -> None:
        """Invoke the confirm method with resolved arguments.

        Raises:
            ValueError: If the participant has no confirm_method defined.
        """
        method = participant_def.confirm_method
        if method is None:
            raise ValueError(f"Participant '{participant_def.id}' has no confirm_method defined.")

        kwargs = self._argument_resolver.resolve(
            method,
            bean,
            ctx,
            input_data=None,
            participant_id=participant_def.id,
        )
        await self._call(method, bean, kwargs)

    async def invoke_cancel(
        self,
        participant_def: ParticipantDefinition,
        bean: Any,
        ctx: TccContext,
    ) -> None:
        """Invoke the cancel method with resolved arguments.

        Raises:
            ValueError: If the participant has no cancel_method defined.
        """
        method = participant_def.cancel_method
        if method is None:
            raise ValueError(f"Participant '{participant_def.id}' has no cancel_method defined.")

        kwargs = self._argument_resolver.resolve(
            method,
            bean,
            ctx,
            input_data=None,
            participant_id=participant_def.id,
        )
        await self._call(method, bean, kwargs)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _call(
        method: Callable[..., Any],
        bean: Any,
        kwargs: dict[str, Any],
    ) -> Any:
        """Call *method* with *bean* as ``self`` and the resolved *kwargs*."""
        if inspect.iscoroutinefunction(method):
            return await method(bean, **kwargs)

        # Synchronous call.
        return method(bean, **kwargs)
