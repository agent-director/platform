# Placeholder Dockerfile for all microservices
# To be replaced with actual multi-stage builds in the next epic.
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*


RUN echo "Microservice pending implementation."

# Keep the container running so Kubernetes doesn't crash-loop it immediately
CMD ["sh", "-c", "echo 'Microservice stub running...'; while true; do sleep 3600; done"]
