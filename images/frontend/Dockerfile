# syntax=docker/dockerfile:1
FROM node:26.10.0-trixie-slim@sha256:930557a230abacbc3f4fd9b8648abf8f4bee1e17cb72195dcdfb2f709bc85b33 AS builder
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci
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
