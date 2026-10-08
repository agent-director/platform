# syntax=docker/dockerfile:1
# Placeholder Dockerfile for all microservices
# To be replaced with actual multi-stage builds in the next epic.
FROM ghcr.io/astral-sh/uv:trixie-slim@sha256:cb8750e12902fdaca60c1e48a796d19c02e6c2037ca801f322689fa97f0a44f2 AS builder
ENV UV_PYTHON_INSTALL_DIR=/opt/python
WORKDIR /app
COPY .python-version ./
COPY README.md pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev && \
    rm -rf /opt/python/*/lib/python*/site-packages/pip* /opt/python/*/lib/python*/site-packages/setuptools* /opt/python/*/bin/pip*

FROM gcr.io/distroless/base-debian13@sha256:389cad21f73e4c37b94ffe5b13736d5a92bd5bd3c6c6b38c2be1c881e14ba2bd
WORKDIR /app
COPY --from=builder /opt/python /opt/python
COPY --from=builder /app/.venv /app/.venv
COPY orchestrator /app/orchestrator
ENV PATH="/app/.venv/bin:$PATH"
USER 65532:65532

ENTRYPOINT ["python", "-m", "orchestrator.temporal_worker"]
