# Builder stage
FROM docker.io/library/golang:1.27.1-trixie@sha256:3b77fc618ec235a1ab412de7737f120dd507c57e8d87de4cbb7994fb94275ed5 AS builder

# Single source of truth for upstream tracking
# renovate: datasource=github-tags depName=agent-substrate/substrate
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
RUN echo "substrate:x:10001:10001::/home/substrate:/sbin/nologin" >> /etc/passwd && \
    echo "substrate:x:10001:" >> /etc/group && \
    mkdir -p /home/substrate && chown 10001:10001 /home/substrate
USER 10001
ENTRYPOINT ["/usr/local/bin/ateapi"]

# atecontroller image
FROM registry.access.redhat.com/ubi9/ubi-minimal:9.5@sha256:8ebe2ad8fdf3cab3e5a53c1edc69194c98209cfadab24b884f4ad9ebcf7bbbfc AS atecontroller
COPY --from=builder /out/atecontroller /usr/local/bin/atecontroller
# hadolint ignore=DL3041
RUN echo "substrate:x:10001:10001::/home/substrate:/sbin/nologin" >> /etc/passwd && \
    echo "substrate:x:10001:" >> /etc/group && \
    mkdir -p /home/substrate && chown 10001:10001 /home/substrate
USER 10001
ENTRYPOINT ["/usr/local/bin/atecontroller"]
