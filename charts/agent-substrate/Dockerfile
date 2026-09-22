# Builder stage
FROM golang:1.23-bookworm AS builder

# Install git
RUN apt-get update && apt-get install -y git

WORKDIR /build
# Clone the upstream agent-substrate repository
RUN git clone https://github.com/google/agent-substrate.git .

# Build the binaries
RUN CGO_ENABLED=0 GOOS=linux go build -o /out/ateapi ./cmd/ateapi
RUN CGO_ENABLED=0 GOOS=linux go build -o /out/atelet ./cmd/atelet
RUN CGO_ENABLED=0 GOOS=linux go build -o /out/atecontroller ./cmd/atecontroller
RUN CGO_ENABLED=0 GOOS=linux go build -o /out/ateom-gvisor ./cmd/ateom-gvisor
RUN CGO_ENABLED=0 GOOS=linux go build -o /out/ateom-microvm ./cmd/ateom-microvm

# ateapi image
FROM registry.access.redhat.com/ubi9/ubi-minimal AS ateapi
COPY --from=builder /out/ateapi /usr/local/bin/ateapi
ENTRYPOINT ["/usr/local/bin/ateapi"]

# atelet image
FROM registry.access.redhat.com/ubi9/ubi-minimal AS atelet
COPY --from=builder /out/atelet /usr/local/bin/atelet
ENTRYPOINT ["/usr/local/bin/atelet"]

# atecontroller image
FROM registry.access.redhat.com/ubi9/ubi-minimal AS atecontroller
COPY --from=builder /out/atecontroller /usr/local/bin/atecontroller
ENTRYPOINT ["/usr/local/bin/atecontroller"]

# ateom-gvisor image
FROM registry.access.redhat.com/ubi9/ubi-minimal AS ateom-gvisor
COPY --from=builder /out/ateom-gvisor /usr/local/bin/ateom-gvisor
ENTRYPOINT ["/usr/local/bin/ateom-gvisor"]

# ateom-microvm image
FROM registry.access.redhat.com/ubi9/ubi-minimal AS ateom-microvm
COPY --from=builder /out/ateom-microvm /usr/local/bin/ateom-microvm
ENTRYPOINT ["/usr/local/bin/ateom-microvm"]
