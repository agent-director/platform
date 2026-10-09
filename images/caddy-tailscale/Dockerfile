FROM caddy:builder-alpine AS builder
# hadolint ignore=DL3018
RUN apk add --no-cache mailcap && \
    xcaddy build --output /usr/bin/caddy --with github.com/tailscale/caddy-tailscale
SHELL ["/bin/ash", "-eo", "pipefail", "-c"]
RUN /usr/bin/caddy version && /usr/bin/caddy list-modules | grep tailscale

FROM scratch
COPY --from=builder /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/
COPY --from=builder /etc/mime.types /etc/mime.types
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
USER 1000:1000
CMD ["/usr/bin/caddy", "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"]
