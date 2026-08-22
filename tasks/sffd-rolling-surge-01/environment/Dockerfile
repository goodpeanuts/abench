FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/sffd-task/environment:${PATH} \
    EMS_SOCKET=/tmp/ems/emsctl.sock \
    EMS_SIDECAR=1 \
    EMS_STATE_DIR=/tmp/ems-state

WORKDIR /opt/sffd-task

# This is the Agent-facing image.  The task root (in particular the hidden
# scenario/calibration and verifier) is intentionally not copied here; the
# simulator sidecar receives those files through a separate read-only mount.
COPY emsctl /opt/sffd-task/environment/emsctl
COPY public_contract.json /opt/sffd-task/environment/public_contract.json

# The first task intentionally has no third-party runtime dependency.
RUN chmod +x environment/emsctl \
    && addgroup --system harbor \
    && adduser --system --ingroup harbor agent \
    && mkdir -p /tmp/ems /tmp/ems-state \
    && chown -R agent:harbor /tmp/ems /tmp/ems-state

USER agent
CMD ["python3", "-c", "import time; time.sleep(2147483647)"]
