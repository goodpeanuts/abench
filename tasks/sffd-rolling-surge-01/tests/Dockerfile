FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SFFD_HARBOR_VERIFIER=1

# Harbor builds a separate verifier image from this directory and does not
# upload tests/ at runtime in separate mode.  Keep the complete verifier
# helper set in /tests; hidden scenario/simulator artifacts are transferred
# later at their original absolute paths.
COPY . /tests/

RUN addgroup --system verifier \
    && adduser --system --ingroup verifier verifier \
    && chmod 0555 /tests/*.sh \
    && find /tests -type f -name '*.py' -exec chmod 0444 {} + \
    && mkdir -p /logs/verifier

WORKDIR /tests
