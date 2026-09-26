# syntax=docker/dockerfile:1
FROM node:20-bookworm-slim AS deps
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci

FROM node:20-bookworm-slim AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY frontend/ .
# Inject API URL at build time or runtime?
# Using Next.js standalone output for smaller footprint
RUN npm run build

FROM node:20-bookworm-slim AS runner
WORKDIR /app

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1

# Upgrade OS packages to fix base image CVEs, and strip npm to fix the tar CVE
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/* && \
    rm -rf /usr/local/lib/node_modules/npm /usr/local/bin/npm /usr/local/bin/npx

RUN groupadd --system --gid 1001 nodejs && \
    useradd --system --uid 1001 --gid 1001 nextjs

COPY --from=builder /app/public ./public

# Automatically leverage output traces to reduce image size
# https://nextjs.org/docs/advanced-features/output-file-tracing
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static

USER 1001:1001

EXPOSE 3000
ENV PORT=3000

CMD ["node", "server.js"]
