# syntax=docker/dockerfile:1
FROM node:26-trixie-slim@sha256:ec7758ee051e457b468b32bde57b0879010b325bb9862718e9615225ce4aaae1 AS deps
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci

FROM node:26-trixie-slim@sha256:ec7758ee051e457b468b32bde57b0879010b325bb9862718e9615225ce4aaae1 AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY frontend/ .
RUN npm run build
FROM gcr.io/distroless/nodejs26-debian13@sha256:3535dc89b77aad3c749e90e81b84d5b0af721fe491930294de6d6d2d6a34e67f
WORKDIR /app

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1

COPY --from=builder /app/public ./public
COPY --from=builder --chown=nonroot:nonroot /app/.next/standalone ./
COPY --from=builder --chown=nonroot:nonroot /app/.next/static ./.next/static
# hadolint ignore=DL3066
USER nonroot

EXPOSE 3000
ENV PORT=3000

CMD ["server.js"]
