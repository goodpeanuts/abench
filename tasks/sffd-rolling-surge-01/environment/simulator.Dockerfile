FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/sffd-sidecar \
    EMS_STATE_DIR=/tmp/ems-state \
    EMS_SOCKET=/tmp/ems/emsctl.sock

WORKDIR /opt/sffd-sidecar

# Only the sidecar receives simulator code.  The scenario is injected at
# runtime as a read-only mount by docker-compose/Harbor.
COPY simulator /opt/sffd-sidecar/simulator
COPY simulator_server.py /opt/sffd-sidecar/simulator_server.py

RUN chmod +x /opt/sffd-sidecar/simulator_server.py \
    && addgroup --system harbor \
    && adduser --system --ingroup harbor simulator \
    && mkdir -p /opt/sffd-sidecar /tmp/ems /tmp/ems-state \
    && chown -R simulator:harbor /tmp/ems /tmp/ems-state

USER simulator
CMD ["python3", "simulator_server.py", "--scenario", "/opt/sffd-sidecar/scenario.json", "--socket", "/tmp/ems/emsctl.sock"]
