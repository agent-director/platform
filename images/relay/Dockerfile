# Placeholder Dockerfile for all microservices
# To be replaced with actual multi-stage builds in the next epic.
FROM ghcr.io/astral-sh/uv:trixie-slim AS builder
ENV UV_PYTHON_INSTALL_DIR=/opt/python
WORKDIR /app
RUN uv venv /app/.venv --python 3.14 && \
    rm -rf /opt/python/*/lib/python*/site-packages/pip* /opt/python/*/lib/python*/site-packages/setuptools* /opt/python/*/bin/pip*

# hadolint ignore=DL3007
FROM gcr.io/distroless/base-debian13:latest
WORKDIR /app
COPY --from=builder /opt/python /opt/python
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# Keep the container running using native Python (no shell available in distroless)
ENTRYPOINT ["python", "-c", "import time; print('Microservice stub running...', flush=True); time.sleep(3600000)"]
