# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS builder

WORKDIR /app
COPY pyproject.toml .
# Install dependencies into a virtual environment
RUN uv pip compile pyproject.toml -o requirements.txt && \
    uv venv /opt/venv && \
    VIRTUAL_ENV=/opt/venv uv pip install -r requirements.txt

FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

# hadolint ignore=DL3013
RUN pip install --no-cache-dir --upgrade setuptools wheel jaraco.context msgpack urllib3

ENV PATH="/opt/venv/bin:$PATH"
WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY gateway/unified_api.py /app/gateway/unified_api.py
COPY gateway/shunt_middleware.py /app/gateway/shunt_middleware.py

CMD ["uvicorn", "gateway.unified_api:app", "--host", "0.0.0.0", "--port", "8000"]

WORKDIR /app
COPY pyproject.toml .
# Install dependencies into a virtual environment
RUN uv pip compile pyproject.toml -o requirements.txt && \
    uv venv /opt/venv && \
    VIRTUAL_ENV=/opt/venv uv pip install -r requirements.txt

FROM python:3.14-slim

# hadolint ignore=DL3013
RUN pip install --no-cache-dir --upgrade setuptools wheel jaraco.context msgpack urllib3

ENV PATH="/opt/venv/bin:$PATH"
WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY gateway/unified_api.py /app/gateway/unified_api.py
COPY gateway/shunt_middleware.py /app/gateway/shunt_middleware.py

CMD ["uvicorn", "gateway.unified_api:app", "--host", "0.0.0.0", "--port", "8000"]
