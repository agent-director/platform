# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:trixie-slim@sha256:cb8750e12902fdaca60c1e48a796d19c02e6c2037ca801f322689fa97f0a44f2 AS builder

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_INSTALL_DIR=/opt/python
WORKDIR /app
# Install project dependencies
COPY .python-version pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev && \
    rm -rf /opt/python/*/lib/python*/site-packages/pip* /opt/python/*/lib/python*/site-packages/setuptools* /opt/python/*/bin/pip*
# Copy the application code
COPY gateway/ ./gateway/
# Runtime stage: True distroless
FROM gcr.io/distroless/cc-debian13@sha256:159783207c2cd44c2aa5715961d13c8612368ac9bd450f887e3f08fc8ea461e3
WORKDIR /app
# Copy the pre-built environment
COPY --from=builder /opt/python /opt/python
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/gateway /app/gateway
# Make sure the virtualenv is on the path
ENV PATH="/app/.venv/bin:$PATH"
USER 65532:65532
# Run the bare uvicorn binary directly from the virtual environment
ENTRYPOINT ["uvicorn", "gateway.unified_api:app", "--host", "0.0.0.0", "--port", "8000"]
