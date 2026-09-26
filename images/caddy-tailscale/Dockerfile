FROM caddy:2.11-builder@sha256:7a05631b5e68fa24cfda0d4392b9c8577715c855b9b9da84b1b59be87e535405 AS builder

RUN xcaddy build \
    --with github.com/tailscale/caddy-tailscale

FROM caddy:2.11@sha256:040e9f7480b80b6d4a7e5013a21159b950a63dcbdb956e38abe2387fb28d9ec0
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
RUN apt-get update && \
    apt-get upgrade -y && \
    apt-get install -y --no-install-recommends libcap2-bin=1:2.66-4 && \
    setcap -r /usr/bin/caddy || true && \
    rm -rf /var/lib/apt/lists/*

USER 1000:1000
