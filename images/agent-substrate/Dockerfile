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

RUN echo "substrate:x:10001:10001::/home/substrate:/sbin/nologin" > /out/passwd && \
    echo "substrate:x:10001:" > /out/group

# ateapi image
FROM scratch AS ateapi
COPY --from=builder /out/passwd /etc/passwd
COPY --from=builder /out/group /etc/group
COPY --from=builder /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/
COPY --from=builder /out/ateapi /usr/local/bin/ateapi
USER 10001
ENTRYPOINT ["/usr/local/bin/ateapi"]

# atecontroller image
FROM scratch AS atecontroller
COPY --from=builder /out/passwd /etc/passwd
COPY --from=builder /out/group /etc/group
COPY --from=builder /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/
COPY --from=builder /out/atecontroller /usr/local/bin/atecontroller
USER 10001
ENTRYPOINT ["/usr/local/bin/atecontroller"]
