# syntax=docker/dockerfile:1
# Exact image/tool versions are supplied by scripts/toolchain.py from root mise.
ARG NODE_IMAGE
ARG PNPM_IMAGE
ARG NODE_RUNTIME_IMAGE
FROM ${PNPM_IMAGE} AS pnpm
FROM ${NODE_IMAGE} AS builder
ARG NODE_VERSION
ARG PNPM_VERSION
WORKDIR /app
ENV PLATFORM_CACHE_DIR=/app/.cache
ENV pnpm_config_state_dir="${PLATFORM_CACHE_DIR}/pnpm/state"
ENV XDG_RUNTIME_DIR="${PLATFORM_CACHE_DIR}/xdg-runtime"
ENV NEXT_TELEMETRY_DISABLED=1
# The official pnpm image supplies the native binary, not a Node.js runtime.
COPY --from=pnpm /opt/pnpm /opt/pnpm
ENV PATH="/opt/pnpm:${PATH}"
RUN mkdir -p "${XDG_RUNTIME_DIR}" && chmod 700 "${XDG_RUNTIME_DIR}" \
    && test "$(node --version)" = "v${NODE_VERSION}" \
    && test "$(pnpm --version)" = "${PNPM_VERSION}"
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
COPY frontend/package.json ./frontend/package.json
RUN pnpm install --frozen-lockfile
# Do not overwrite locked dependencies or bake local .env files into the image.
COPY frontend/next.config.js frontend/tsconfig.json ./frontend/
COPY frontend/src ./frontend/src
COPY frontend/public ./frontend/public
RUN pnpm --filter platform-ui build

FROM ${NODE_RUNTIME_IMAGE} AS runtime
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
