FROM node:20-slim@sha256:2cf067cfed83d5ea958367df9f966191a942351a2df77d6f0193e162b5febfc0 AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM node:20-slim@sha256:2cf067cfed83d5ea958367df9f966191a942351a2df77d6f0193e162b5febfc0 AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# Plafond de tas pour le build (défaut 2,5 Gio). Sur cette machine, le modèle
# de chat servi occupe déjà ~105 Gio des 121 : un build non borné peut pousser
# le noyau à échanger, et l'OOM-killer désigne alors le plus gros RSS — c'est-
# à-dire le modèle en cours de service, ce qui est interdit. Une borne
# transforme un risque non borné en échec de build borné. Relever la valeur
# (docker build --build-arg NODE_HEAP_MB=4096) si le build manque de mémoire.
ARG NODE_HEAP_MB=2560
ENV NODE_OPTIONS=--max-old-space-size=${NODE_HEAP_MB}
RUN npm run build

FROM node:20-slim@sha256:2cf067cfed83d5ea958367df9f966191a942351a2df77d6f0193e162b5febfc0 AS runner
WORKDIR /app
ENV NODE_ENV=production
# Utilisateur non-root, même schéma que dgx-portal/Dockerfile.
RUN groupadd -r frontend && useradd -r -u 10002 -g frontend frontend
COPY --from=builder /app/public ./public
COPY --from=builder --chown=frontend:frontend /app/.next/standalone ./
COPY --from=builder --chown=frontend:frontend /app/.next/static ./.next/static
USER frontend
EXPOSE 3000
ENV PORT=3000 HOSTNAME=0.0.0.0
CMD ["node", "server.js"]
