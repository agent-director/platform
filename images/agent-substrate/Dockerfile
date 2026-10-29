# Builder stage
FROM docker.io/library/golang:1.27.3-trixie@sha256:fbb7ae40b62ff8afe31662df570d39b4f5052ac32b90dae8e60d7657a20a74f4 AS builder

# Single source of truth for upstream tracking
# renovate: datasource=github-tags depName=agent-substrate/substrate
ARG AGENT_SUBSTRATE_VERSION=v0.4.0

WORKDIR /build
# Clone the upstream agent-substrate repository
RUN git clone --depth 1 -b ${AGENT_SUBSTRATE_VERSION} https://github.com/agent-substrate/substrate.git .

# Build the binaries
# Build the binaries with GOTOOLCHAIN=auto to natively fetch the required Go 1.24+ compiler
RUN go get golang.org/x/net@v0.60.0 github.com/go-jose/go-jose/v4@v4.1.6 && go mod tidy && go mod vendor && \
    GOTOOLCHAIN=auto CGO_ENABLED=0 GOOS=linux go build -o /out/ateapi ./cmd/ateapi && \
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
