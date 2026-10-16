# syntax=docker/dockerfile:1
FROM ghcr.io/pnpm/pnpm:12.11.2@sha256:fd95b696aff515e5c58f59248435c29d783fce85cfc9a28c9db3b994bffa44dd AS pnpm
FROM node:26.11.1-trixie-slim@sha256:193fe51b64e77981119c98c2002c9e32a70e2f006fb4d25068ce0558998917f0 AS builder
WORKDIR /app
ENV PLATFORM_CACHE_DIR=/app/.cache
ENV pnpm_config_state_dir="${PLATFORM_CACHE_DIR}/pnpm/state"
ENV XDG_RUNTIME_DIR="${PLATFORM_CACHE_DIR}/xdg-runtime"
ENV NEXT_TELEMETRY_DISABLED=1
# The official pnpm image supplies the native binary, not a Node.js runtime.
COPY --from=pnpm /opt/pnpm /opt/pnpm
ENV PATH="/opt/pnpm:${PATH}"
RUN mkdir -p "${XDG_RUNTIME_DIR}" && chmod 700 "${XDG_RUNTIME_DIR}"
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
COPY frontend/package.json ./frontend/package.json
RUN pnpm install --frozen-lockfile
# Do not overwrite locked dependencies or bake local .env files into the image.
COPY frontend/next.config.js frontend/tsconfig.json ./frontend/
COPY frontend/src ./frontend/src
COPY frontend/public ./frontend/public
RUN pnpm --filter platform-ui build

FROM gcr.io/distroless/nodejs26-debian13:nonroot@sha256:a3bb283a564754266646d6029c5b6aa4f760b9415dae1398db8d7f8142ee1375 AS runtime
# Distroless currently ships an older Node patch; use the exact selected binary
# without adding a shell or package manager to the hardened runtime.
COPY --from=builder /usr/local/bin/node /nodejs/bin/node
WORKDIR /app
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
ENV HOSTNAME=0.0.0.0
ENV PORT=3000
# SSR chunks live under the root cache; pnpm's app links are not their ancestors.
ENV NODE_PATH=/app/frontend/node_modules

COPY --from=builder --chown=nonroot:nonroot /app/frontend/public ./frontend/public
COPY --from=builder --chown=nonroot:nonroot /app/.cache/frontend-next/standalone ./
COPY --from=builder --chown=nonroot:nonroot /app/.cache/frontend-next/static ./.cache/frontend-next/static
# hadolint ignore=DL3066
USER nonroot
EXPOSE 3000
CMD ["frontend/server.js"]
