# Builder stage
FROM docker.io/library/golang:1.27.1-trixie@sha256:167053a2bb901972bf2c1611f8f52c44d5fe7e762e5cab213708d82c421614db AS builder

# Install git
RUN apt-get update && apt-get install -y --no-install-recommends git=1:2.* && rm -rf /var/lib/apt/lists/*

WORKDIR /build
# Clone the upstream agent-substrate repository
RUN git clone --depth 1 -b v0.1.0 https://github.com/agent-substrate/substrate.git .

# Build the binaries
RUN CGO_ENABLED=0 GOOS=linux go build -o /out/ateapi ./cmd/ateapi && \
    CGO_ENABLED=0 GOOS=linux go build -o /out/atelet ./cmd/atelet && \
    CGO_ENABLED=0 GOOS=linux go build -o /out/atecontroller ./cmd/atecontroller && \
    CGO_ENABLED=0 GOOS=linux go build -o /out/ateom-microvm ./cmd/ateom-microvm

# ateapi image
FROM registry.access.redhat.com/ubi9/ubi-minimal:9.5@sha256:8ebe2ad8fdf3cab3e5a53c1edc69194c98209cfadab24b884f4ad9ebcf7bbbfc AS ateapi
COPY --from=builder /out/ateapi /usr/local/bin/ateapi
RUN microdnf install -y shadow-utils-4.9.0-1 && useradd -u 10001 -m substrate && microdnf clean all
USER 10001
ENTRYPOINT ["/usr/local/bin/ateapi"]

# atelet image
FROM registry.access.redhat.com/ubi9/ubi-minimal:9.5@sha256:8ebe2ad8fdf3cab3e5a53c1edc69194c98209cfadab24b884f4ad9ebcf7bbbfc AS atelet
COPY --from=builder /out/atelet /usr/local/bin/atelet
RUN microdnf install -y shadow-utils-4.9.0-1 && useradd -u 10001 -m substrate && microdnf clean all
USER 10001
ENTRYPOINT ["/usr/local/bin/atelet"]

# atecontroller image
FROM registry.access.redhat.com/ubi9/ubi-minimal:9.5@sha256:8ebe2ad8fdf3cab3e5a53c1edc69194c98209cfadab24b884f4ad9ebcf7bbbfc AS atecontroller
COPY --from=builder /out/atecontroller /usr/local/bin/atecontroller
RUN microdnf install -y shadow-utils-4.9.0-1 && useradd -u 10001 -m substrate && microdnf clean all
USER 10001
ENTRYPOINT ["/usr/local/bin/atecontroller"]

# ateom-microvm image
FROM registry.access.redhat.com/ubi9/ubi-minimal:9.5@sha256:8ebe2ad8fdf3cab3e5a53c1edc69194c98209cfadab24b884f4ad9ebcf7bbbfc AS ateom-microvm
COPY --from=builder /out/ateom-microvm /usr/local/bin/ateom-microvm
RUN microdnf install -y shadow-utils-4.9.0-1 && useradd -u 10001 -m substrate && microdnf clean all
USER 10001
ENTRYPOINT ["/usr/local/bin/ateom-microvm"]
