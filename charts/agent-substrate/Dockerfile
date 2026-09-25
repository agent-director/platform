# Builder stage
# hadolint ignore=DL3007
FROM docker.io/library/golang:latest AS builder

# Install git
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

WORKDIR /build
# Clone the upstream agent-substrate repository
RUN git clone --depth 1 -b v0.1.0 https://github.com/agent-substrate/substrate.git .

# Build the binaries
RUN CGO_ENABLED=0 GOOS=linux go build -o /out/ateapi ./cmd/ateapi && \
    CGO_ENABLED=0 GOOS=linux go build -o /out/atelet ./cmd/atelet && \
    CGO_ENABLED=0 GOOS=linux go build -o /out/atecontroller ./cmd/atecontroller && \
    CGO_ENABLED=0 GOOS=linux go build -o /out/ateom-microvm ./cmd/ateom-microvm

# ateapi image
# hadolint ignore=DL3007
FROM registry.access.redhat.com/ubi9/ubi-minimal:latest AS ateapi
COPY --from=builder /out/ateapi /usr/local/bin/ateapi
# hadolint ignore=DL3041
RUN microdnf install -y shadow-utils && useradd -u 10001 -m substrate && microdnf clean all
USER 10001
ENTRYPOINT ["/usr/local/bin/ateapi"]

# atelet image
# hadolint ignore=DL3007
FROM registry.access.redhat.com/ubi9/ubi-minimal:latest AS atelet
COPY --from=builder /out/atelet /usr/local/bin/atelet
# hadolint ignore=DL3041
RUN microdnf install -y shadow-utils && useradd -u 10001 -m substrate && microdnf clean all
USER 10001
ENTRYPOINT ["/usr/local/bin/atelet"]

# atecontroller image
# hadolint ignore=DL3007
FROM registry.access.redhat.com/ubi9/ubi-minimal:latest AS atecontroller
COPY --from=builder /out/atecontroller /usr/local/bin/atecontroller
# hadolint ignore=DL3041
RUN microdnf install -y shadow-utils && useradd -u 10001 -m substrate && microdnf clean all
USER 10001
ENTRYPOINT ["/usr/local/bin/atecontroller"]

# ateom-microvm image
# hadolint ignore=DL3007
FROM registry.access.redhat.com/ubi9/ubi-minimal:latest AS ateom-microvm
COPY --from=builder /out/ateom-microvm /usr/local/bin/ateom-microvm
# hadolint ignore=DL3041
RUN microdnf install -y shadow-utils && useradd -u 10001 -m substrate && microdnf clean all
USER 10001
ENTRYPOINT ["/usr/local/bin/ateom-microvm"]
