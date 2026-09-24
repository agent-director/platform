# Builder stage
FROM golang:1.23-bookworm AS builder

# Install git
RUN apt-get update && apt-get install -y git

WORKDIR /build
# Clone the upstream agent-substrate repository
RUN git clone --depth 1 -b v0.1.0 https://github.com/agent-substrate/substrate.git .

# Build the binaries
RUN CGO_ENABLED=0 GOOS=linux go build -o /out/ateapi ./cmd/ateapi && \
    CGO_ENABLED=0 GOOS=linux go build -o /out/atelet ./cmd/atelet && \
    CGO_ENABLED=0 GOOS=linux go build -o /out/atecontroller ./cmd/atecontroller && \
    CGO_ENABLED=0 GOOS=linux go build -o /out/ateom-microvm ./cmd/ateom-microvm

# ateapi image
FROM registry.access.redhat.com/ubi9/ubi-minimal AS ateapi
COPY --from=builder /out/ateapi /usr/local/bin/ateapi
RUN microdnf install -y shadow-utils && useradd -u 10001 -m substrate && microdnf clean all
USER 10001
ENTRYPOINT ["/usr/local/bin/ateapi"]

# atelet image
FROM registry.access.redhat.com/ubi9/ubi-minimal AS atelet
COPY --from=builder /out/atelet /usr/local/bin/atelet
RUN microdnf install -y shadow-utils && useradd -u 10001 -m substrate && microdnf clean all
USER 10001
ENTRYPOINT ["/usr/local/bin/atelet"]

# atecontroller image
FROM registry.access.redhat.com/ubi9/ubi-minimal AS atecontroller
COPY --from=builder /out/atecontroller /usr/local/bin/atecontroller
RUN microdnf install -y shadow-utils && useradd -u 10001 -m substrate && microdnf clean all
USER 10001
ENTRYPOINT ["/usr/local/bin/atecontroller"]

# ateom-microvm image
FROM registry.access.redhat.com/ubi9/ubi-minimal AS ateom-microvm
COPY --from=builder /out/ateom-microvm /usr/local/bin/ateom-microvm
RUN microdnf install -y shadow-utils && useradd -u 10001 -m substrate && microdnf clean all
USER 10001
ENTRYPOINT ["/usr/local/bin/ateom-microvm"]
