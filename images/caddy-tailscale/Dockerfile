FROM caddy:2.11.7-builder@sha256:b25f47453fa02f7e66c0828b4b1343658808b9000950b6825c4a1cb0c988f5f8 AS builder
# hadolint ignore=DL3018
RUN apk add --no-cache mailcap && \
    mv /usr/local/go/bin/go /usr/local/go/bin/go.real && \
    echo '#!/bin/sh' > /usr/local/go/bin/go && \
    echo 'if echo "$*" | grep -q "build"; then /usr/local/go/bin/go.real get golang.org/x/net@v0.60.0 && /usr/local/go/bin/go.real mod tidy; fi' >> /usr/local/go/bin/go && \
    echo 'exec /usr/local/go/bin/go.real "$@"' >> /usr/local/go/bin/go && \
    chmod +x /usr/local/go/bin/go && \
    xcaddy build --output /usr/bin/caddy \
    --with github.com/tailscale/caddy-tailscale \
    --with github.com/corazawaf/coraza-caddy/v2 \
    --with github.com/mholt/caddy-ratelimit && \
    mv /usr/local/go/bin/go.real /usr/local/go/bin/go
SHELL ["/bin/ash", "-eo", "pipefail", "-c"]
RUN /usr/bin/caddy version && /usr/bin/caddy list-modules | grep tailscale && /usr/bin/caddy list-modules | grep "\.waf" && /usr/bin/caddy list-modules | grep rate_limit

FROM scratch
COPY --from=builder /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/
COPY --from=builder /etc/mime.types /etc/mime.types
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
USER 1000:1000
CMD ["/usr/bin/caddy", "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"]
