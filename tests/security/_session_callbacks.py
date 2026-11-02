# Copyright 2026 Firefly Software Foundation.
# SPDX-License-Identifier: Apache-2.0
"""Run callback unit fixtures through the actual native persistence boundary."""

from pyfly.session.adapters.memory import InMemorySessionStore
from pyfly.session.filter import SessionFilter


async def stored_callback(handler, request):
    store = InMemorySessionStore()
    session = request.state.session
    await store.save(session.id, session.get_data(), 60)
    request.scope["headers"] = [(b"cookie", f"PYFLY_SESSION={session.id}".encode())]
    return await SessionFilter(store).do_filter(request, handler._handle_callback)
