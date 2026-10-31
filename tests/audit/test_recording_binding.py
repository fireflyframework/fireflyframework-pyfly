# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0

import pytest

from pyfly.audit import AuditError
from pyfly.audit.recording_binding import AuditModulePostProcessor, AuditRecordingSupport


def test_native_binding_is_lazy_and_cannot_rebind_shared_instances():
    calls = []
    target = AuditRecordingSupport()

    def factory():
        calls.append(True)
        return object()

    processor = AuditModulePostProcessor(factory)
    assert processor.after_init(target, "fixture") is target
    assert calls == []
    with pytest.raises(AuditError, match="another application"):
        AuditModulePostProcessor(lambda: object()).after_init(target, "other")
