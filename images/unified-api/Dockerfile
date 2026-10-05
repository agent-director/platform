# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app

# Install project dependencies
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev
# Copy the application code
COPY gateway/ ./gateway/

# Runtime stage
FROM python:3.14-slim

# Strip the globally bundled pip/setuptools/wheel to resolve CVEs and drastically reduce production attack surface
RUN pip uninstall -y pip setuptools wheel || true

WORKDIR /app

# Copy the pre-built environment
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/gateway /app/gateway

# Make sure the virtualenv is on the path
ENV PATH="/app/.venv/bin:$PATH"

# Run the bare uvicorn binary directly from the virtual environment (uv run not needed in runtime)
CMD ["uvicorn", "gateway.unified_api:app", "--host", "0.0.0.0", "--port", "8000"]
