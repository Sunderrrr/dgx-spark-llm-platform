FROM node:20-slim@sha256:2cf067cfed83d5ea958367df9f966191a942351a2df77d6f0193e162b5febfc0 AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM node:20-slim@sha256:2cf067cfed83d5ea958367df9f966191a942351a2df77d6f0193e162b5febfc0 AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# Heap ceiling for the build (default 2.5 GiB). On this machine the served chat
# model already holds ~105 GiB of the 121: an unbounded build can push the
# kernel into swapping, and the OOM killer then picks the biggest RSS — that is,
# the model being served, which is forbidden. A bound turns an unbounded risk
# into a bounded build failure. Raise the value (docker build --build-arg
# NODE_HEAP_MB=4096) if the build runs out of memory.
ARG NODE_HEAP_MB=2560
ENV NODE_OPTIONS=--max-old-space-size=${NODE_HEAP_MB}
# next.config.ts bakes the fallback rewrite to portal-rs at build time, so the
# address must be known here, not only at run time (compose always passes it).
ARG BACKEND_URL=
RUN npm run build

FROM node:20-slim@sha256:2cf067cfed83d5ea958367df9f966191a942351a2df77d6f0193e162b5febfc0 AS runner
WORKDIR /app
ENV NODE_ENV=production
# Non-root user, same scheme as Dockerfile.portal-rs.
RUN groupadd -r frontend && useradd -r -u 10002 -g frontend frontend
COPY --from=builder /app/public ./public
COPY --from=builder --chown=frontend:frontend /app/.next/standalone ./
COPY --from=builder --chown=frontend:frontend /app/.next/static ./.next/static
USER frontend
EXPOSE 3000
ENV PORT=3000 HOSTNAME=0.0.0.0
CMD ["node", "server.js"]
