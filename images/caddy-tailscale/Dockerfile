FROM caddy:2.11.7-builder@sha256:f5b1a66449d305280e559dba0ab9f7ce9a2a14c527c79d7c6fb48abc8f895818 AS builder
RUN xcaddy build --output /usr/bin/caddy --with github.com/tailscale/caddy-tailscale
SHELL ["/bin/ash", "-eo", "pipefail", "-c"]
RUN /usr/bin/caddy version && /usr/bin/caddy list-modules | grep tailscale

FROM alpine:3.21 AS certs
# hadolint ignore=DL3018
RUN apk add --no-cache ca-certificates mailcap

FROM scratch
COPY --from=certs /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/
COPY --from=certs /etc/mime.types /etc/mime.types
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
USER 1000:1000
CMD ["/usr/bin/caddy", "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"]
