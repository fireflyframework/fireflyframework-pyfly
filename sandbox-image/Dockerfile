# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
FROM python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159
COPY src/pyfly/agentic/sandbox/runner.py /opt/pyfly/runner.py
USER 65532:65532
WORKDIR /tmp
ENTRYPOINT ["python", "-I", "-u", "/opt/pyfly/runner.py"]
