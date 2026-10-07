# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:trixie-slim AS builder

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_INSTALL_DIR=/opt/python
WORKDIR /app

# Install project dependencies
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev --python 3.14 && \
    rm -rf /opt/python/*/lib/python*/site-packages/pip* /opt/python/*/lib/python*/site-packages/setuptools* /opt/python/*/bin/pip*
# Copy the application code
COPY gateway/ ./gateway/

# Runtime stage: True distroless
# hadolint ignore=DL3007
FROM gcr.io/distroless/base-debian13:latest

WORKDIR /app

# Copy the pre-built environment
COPY --from=builder /opt/python /opt/python
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/gateway /app/gateway

# Make sure the virtualenv is on the path
ENV PATH="/app/.venv/bin:$PATH"

# Run the bare uvicorn binary directly from the virtual environment
ENTRYPOINT ["uvicorn", "gateway.unified_api:app", "--host", "0.0.0.0", "--port", "8000"]
