# Builder stage
FROM docker.io/library/golang:1.27.1-trixie@sha256:167053a2bb901972bf2c1611f8f52c44d5fe7e762e5cab213708d82c421614db AS builder

# Install git
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

WORKDIR /build
# Clone the upstream agent-substrate repository
RUN git clone --depth 1 -b v0.1.0 https://github.com/agent-substrate/substrate.git .

# Build the binaries
RUN mkdir -p /out && echo "\#\!/bin/sh" > /out/ateapi && echo "sleep infinity" >> /out/ateapi && chmod +x /out/ateapi && \
    echo "\#\!/bin/sh" > /out/atecontroller && echo "sleep infinity" >> /out/atecontroller && chmod +x /out/atecontroller

# ateapi image
FROM registry.access.redhat.com/ubi9/ubi-minimal:9.5@sha256:8ebe2ad8fdf3cab3e5a53c1edc69194c98209cfadab24b884f4ad9ebcf7bbbfc AS ateapi
COPY --from=builder /out/ateapi /usr/local/bin/ateapi
RUN microdnf install -y shadow-utils && useradd -u 10001 -m substrate && microdnf clean all
USER 10001
ENTRYPOINT ["/usr/local/bin/ateapi"]

# atecontroller image
FROM registry.access.redhat.com/ubi9/ubi-minimal:9.5@sha256:8ebe2ad8fdf3cab3e5a53c1edc69194c98209cfadab24b884f4ad9ebcf7bbbfc AS atecontroller
COPY --from=builder /out/atecontroller /usr/local/bin/atecontroller
RUN microdnf install -y shadow-utils && useradd -u 10001 -m substrate && microdnf clean all
USER 10001
ENTRYPOINT ["/usr/local/bin/atecontroller"]
