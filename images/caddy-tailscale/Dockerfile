FROM caddy:2.11.8-builder@sha256:d4eb070c36154bd2d4c94b40359a03c6168346d5af5404963c74691ab77fb63f AS builder
# hadolint ignore=DL3018
RUN apk add --no-cache mailcap && \
    mv /usr/local/go/bin/go /usr/local/go/bin/go.real && \
    echo '#!/bin/sh' > /usr/local/go/bin/go && \
    echo 'if echo "$*" | grep -q "build"; then /usr/local/go/bin/go.real get golang.org/x/net@v0.60.0 github.com/go-jose/go-jose/v4@v4.1.6 && /usr/local/go/bin/go.real mod tidy; fi' >> /usr/local/go/bin/go && \
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
