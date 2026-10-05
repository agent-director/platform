# Placeholder Dockerfile for all microservices
# To be replaced with actual multi-stage builds in the next epic.
FROM ghcr.io/astral-sh/uv:trixie-slim AS builder
ENV UV_PYTHON_INSTALL_DIR=/opt/python
RUN uv venv /app/.venv --python 3.14 && \
    rm -rf /opt/python/*/lib/python*/site-packages/pip* /opt/python/*/lib/python*/site-packages/setuptools* /opt/python/*/bin/pip*

FROM debian:trixie-slim
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*
COPY --from=builder /opt/python /opt/python
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

RUN echo "Microservice pending implementation."

# Keep the container running so Kubernetes doesn't crash-loop it immediately
CMD ["sh", "-c", "echo 'Microservice stub running...'; while true; do sleep 3600; done"]
