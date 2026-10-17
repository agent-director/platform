# syntax=docker/dockerfile:1
FROM gcr.io/distroless/nodejs26-debian13:nonroot@sha256:a3bb283a564754266646d6029c5b6aa4f760b9415dae1398db8d7f8142ee1375 AS runtime-base
FROM ghcr.io/pnpm/pnpm:12.11.2@sha256:fd95b696aff515e5c58f59248435c29d783fce85cfc9a28c9db3b994bffa44dd AS builder
WORKDIR /app
ENV PLATFORM_CACHE_DIR=/app/.cache
ENV pnpm_config_state_dir="${PLATFORM_CACHE_DIR}/pnpm/state"
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=runtime-base /nodejs /nodejs
ENV PATH="/nodejs/bin:/opt/pnpm:${PATH}"
COPY frontend/package.json frontend/pnpm-workspace.yaml frontend/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/next.config.js frontend/tsconfig.json ./
COPY frontend/src ./src
COPY frontend/public ./public
RUN pnpm run build

FROM runtime-base AS runtime
WORKDIR /app
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
ENV HOSTNAME=0.0.0.0
ENV PORT=3000

COPY --from=builder --chown=65532:65532 /app/public ./public
COPY --from=builder --chown=65532:65532 /app/.next/standalone ./
COPY --from=builder --chown=65532:65532 /app/.next/static ./.next/static
USER 65532:65532
EXPOSE 3000
CMD ["server.js"]
