FROM caddy:2.11.7-builder@sha256:f5b1a66449d305280e559dba0ab9f7ce9a2a14c527c79d7c6fb48abc8f895818 AS builder

RUN xcaddy build --output /usr/bin/caddy --with github.com/tailscale/caddy-tailscale

SHELL ["/bin/ash", "-eo", "pipefail", "-c"]
RUN /usr/bin/caddy version && \
    /usr/bin/caddy list-modules | grep tailscale

FROM caddy:2.11.7@sha256:3422ce6de165df66534f9b9ba50efaf457114ec961763cc52f5dbdaac2972d73
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
# hadolint ignore=DL3018
RUN apk add --no-cache libcap && \
    setcap -r /usr/bin/caddy || true

USER 1000:1000
