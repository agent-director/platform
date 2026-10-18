# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.12.23-trixie-slim@sha256:cb8750e12902fdaca60c1e48a796d19c02e6c2037ca801f322689fa97f0a44f2 AS builder

# renovate: datasource=python-version depName=python
ARG PYTHON_VERSION=3.14.8
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_INSTALL_DIR=/opt/python
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --python "${PYTHON_VERSION}" --no-install-project --no-dev && \
    rm -rf /opt/python/*/lib/python*/site-packages/pip* /opt/python/*/lib/python*/site-packages/setuptools* /opt/python/*/bin/pip*
COPY unified_api/ ./unified_api/

FROM gcr.io/distroless/cc-debian13@sha256:159783207c2cd44c2aa5715961d13c8612368ac9bd450f887e3f08fc8ea461e3
WORKDIR /app
COPY --from=builder /opt/python /opt/python
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/unified_api /app/unified_api
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1
USER 65532:65532
# Uvicorn consumes UVICORN_PORT; Helm owns the listener port.
ENTRYPOINT ["uvicorn", "unified_api.main:app", "--host", "0.0.0.0"]
