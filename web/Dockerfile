FROM node:24-bookworm-slim AS deps
WORKDIR /app
# npm ci installs the exact tree from the committed lock (reproducible builds)
# instead of re-resolving semver ranges on every image build.
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund

FROM node:24-bookworm-slim AS dev
ENV NODE_ENV=development
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY web/ ./
EXPOSE 5173
CMD ["npm", "run", "dev"]

# Production: vite build via adapter-node (self-contained — dependencies are
# rolled into build/), fronted by server.mjs which adds the /api and /go2rtc
# same-origin proxies the dev server provided via Vite's `server.proxy`.
FROM node:24-bookworm-slim AS build
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY web/ ./
RUN npm run build

FROM node:24-bookworm-slim AS prod
ENV NODE_ENV=production
WORKDIR /app
# adapter-node externalises packages (handler.js imports @sveltejs/kit at
# runtime), so the runtime needs node_modules. The tree ships everything in
# `dependencies`, so a --omit=dev install saves nothing — reuse the deps
# stage verbatim.
COPY --from=deps /app/node_modules ./node_modules
COPY --from=build /app/build ./build
# "type": "module" — adapter-node's build/ is ESM; without a package.json
# next to it Node would parse handler.js as CommonJS and refuse the imports.
COPY web/package.json ./
COPY web/server.mjs web/client-ip.mjs web/proxy.mjs ./
EXPOSE 5173
CMD ["node", "server.mjs"]
