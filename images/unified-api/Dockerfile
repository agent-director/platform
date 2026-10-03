# syntax=docker/dockerfile:1
FROM python:3.11-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:latest@sha256:61d393e44e249f2e4b526b6c7ddcecce245946826e608e11c93ad4f5bba55b21 /uv /bin/uv

WORKDIR /app
COPY pyproject.toml .
# Install dependencies into a virtual environment
RUN uv pip compile pyproject.toml -o requirements.txt && \
    uv venv /opt/venv && \
    VIRTUAL_ENV=/opt/venv uv pip install -r requirements.txt

FROM python:3.11-slim

ENV PATH="/opt/venv/bin:$PATH"
WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY gateway/unified_api.py /app/gateway/unified_api.py
COPY gateway/shunt_middleware.py /app/gateway/shunt_middleware.py

CMD ["uvicorn", "gateway.unified_api:app", "--host", "0.0.0.0", "--port", "8000"]
