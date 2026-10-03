# Builder stage
FROM docker.io/library/golang:1.27.1@sha256:e0174e51e81218523251d85d248a90d24c3d5e81543b4f07a5d66229397db190 AS builder

# Install git
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

# Single source of truth for upstream tracking
ARG AGENT_SUBSTRATE_VERSION=v0.3.0

WORKDIR /build
# Clone the upstream agent-substrate repository
RUN git clone --depth 1 -b ${AGENT_SUBSTRATE_VERSION} https://github.com/agent-substrate/substrate.git .

# Build the binaries
# Build the binaries with GOTOOLCHAIN=auto to natively fetch the required Go 1.24+ compiler
RUN GOTOOLCHAIN=auto CGO_ENABLED=0 GOOS=linux go build -o /out/ateapi ./cmd/ateapi && \
    GOTOOLCHAIN=auto CGO_ENABLED=0 GOOS=linux go build -o /out/atecontroller ./cmd/atecontroller

# ateapi image
FROM registry.access.redhat.com/ubi9/ubi-minimal:9.5@sha256:8ebe2ad8fdf3cab3e5a53c1edc69194c98209cfadab24b884f4ad9ebcf7bbbfc AS ateapi
COPY --from=builder /out/ateapi /usr/local/bin/ateapi
# hadolint ignore=DL3041
RUN microdnf install -y shadow-utils && useradd -u 10001 -m substrate && microdnf clean all
USER 10001
ENTRYPOINT ["/usr/local/bin/ateapi"]

# atecontroller image
FROM registry.access.redhat.com/ubi9/ubi-minimal:9.5@sha256:8ebe2ad8fdf3cab3e5a53c1edc69194c98209cfadab24b884f4ad9ebcf7bbbfc AS atecontroller
COPY --from=builder /out/atecontroller /usr/local/bin/atecontroller
# hadolint ignore=DL3041
RUN microdnf install -y shadow-utils && useradd -u 10001 -m substrate && microdnf clean all
USER 10001
ENTRYPOINT ["/usr/local/bin/atecontroller"]
