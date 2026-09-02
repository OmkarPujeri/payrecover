# PayRecover frontend — multi-stage Next.js 15 build.
#
# docker-compose already expects `build: ./frontend` (added this phase). The
# image is standalone-output so the runtime layer carries only the server and its
# traced dependencies, not the full node_modules tree.
#
# NEXT_PUBLIC_API_BASE is baked at build time because it ships in the client
# bundle, and it must be the address the *browser* can reach — the host-published
# backend port, not the compose service name.

# ---- deps ----
FROM node:22-alpine AS deps
WORKDIR /app
COPY package.json .npmrc ./
RUN npm install

# ---- build ----
FROM node:22-alpine AS build
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
ARG NEXT_PUBLIC_API_BASE=http://localhost:8000
ENV NEXT_PUBLIC_API_BASE=$NEXT_PUBLIC_API_BASE
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

# ---- run ----
FROM node:22-alpine AS run
WORKDIR /app
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=build /app/public ./public
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
EXPOSE 3000
CMD ["node", "server.js"]
