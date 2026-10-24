# syntax=docker/dockerfile:1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
FROM gcr.io/distroless/nodejs26-debian13:nonroot@sha256:3a6f4e6fd37efe00042667d06fd52240f2589ed09b0694fbb3b1bf860030a839 AS runtime-base
FROM ghcr.io/pnpm/pnpm:12.11.2@sha256:fd95b696aff515e5c58f59248435c29d783fce85cfc9a28c9db3b994bffa44dd AS builder
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=runtime-base /nodejs /nodejs
ENV PATH="/nodejs/bin:/opt/pnpm:${PATH}"
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
COPY apps/ui/package.json ./apps/ui/package.json
RUN pnpm install --frozen-lockfile --filter platform-ui...
COPY apps/ui/next.config.js apps/ui/tsconfig.json ./apps/ui/
COPY apps/ui/src ./apps/ui/src
COPY apps/ui/public ./apps/ui/public
RUN pnpm --filter platform-ui run build

FROM runtime-base AS runtime
WORKDIR /app
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
ENV HOSTNAME=0.0.0.0
ENV PORT=3000

COPY --from=builder --chown=65532:65532 /app/apps/ui/public ./apps/ui/public
COPY --from=builder --chown=65532:65532 /app/apps/ui/.next/standalone ./
COPY --from=builder --chown=65532:65532 /app/apps/ui/.next/static ./apps/ui/.next/static
USER 65532:65532
EXPOSE 3000
CMD ["apps/ui/server.js"]
