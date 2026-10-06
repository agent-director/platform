# Placeholder Dockerfile for all microservices
# To be replaced with actual multi-stage builds in the next epic.
FROM ghcr.io/astral-sh/uv:trixie-slim AS builder
WORKDIR /app
RUN uv venv /app/.venv --python 3.14

# hadolint ignore=DL3007
FROM cgr.dev/chainguard/python:latest
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# Keep the container running using native Python (no shell available in distroless)
ENTRYPOINT ["python", "-c", "import time; print('Microservice stub running...', flush=True); time.sleep(3600000)"]
