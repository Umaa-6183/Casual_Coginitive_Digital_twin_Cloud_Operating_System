# ══════════════════════════════════════════════════════════════════════════════
# CCDT Dashboard — Dockerfile
# ══════════════════════════════════════════════════════════════════════════════
# Stage layout:
#   deps         — installs node_modules (cached layer)
#   development  — Vite dev server with HMR (used by docker-compose dev)
#   builder      — builds production bundle (npm run build)
#   runtime      — nginx serves static files (~25 MB final image)
# ══════════════════════════════════════════════════════════════════════════════

# ── Stage 1: install dependencies ─────────────────────────────────────────────
FROM node:20-alpine AS deps

WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci --prefer-offline

# ── Stage 2: development server (Vite HMR) ────────────────────────────────────
FROM node:20-alpine AS development

ARG BUILD_DATE="unknown"
WORKDIR /app

# Copy node_modules from deps stage
COPY --from=deps /app/node_modules ./node_modules
COPY . .

ENV NODE_ENV=development
EXPOSE 3000

CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0", "--port", "3000"]

# ── Stage 3: production build ─────────────────────────────────────────────────
FROM node:20-alpine AS builder

ARG BUILD_DATE="unknown"
ARG GIT_SHA="unknown"
ARG VERSION="dev"

WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .

# Inject build-time variables as Vite env vars
ENV VITE_BUILD_DATE=${BUILD_DATE} \
    VITE_GIT_SHA=${GIT_SHA} \
    VITE_VERSION=${VERSION} \
    NODE_ENV=production

RUN npm run build

# ── Stage 4: nginx runtime ────────────────────────────────────────────────────
FROM nginx:1.27-alpine AS runtime

ARG BUILD_DATE="unknown"
ARG GIT_SHA="unknown"
ARG VERSION="dev"

LABEL org.opencontainers.image.title="CCDT Dashboard" \
      org.opencontainers.image.description="CCDT React/Vite operator dashboard" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.version="${VERSION}"

# Remove default nginx config
RUN rm /etc/nginx/conf.d/default.conf

# Copy nginx config for SPA (single-page app routing)
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Copy built static files from builder
COPY --from=builder /app/dist /usr/share/nginx/html

EXPOSE 3000

HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -qO- http://localhost:3000 || exit 1

CMD ["nginx", "-g", "daemon off;"]
