# SlothPay frontend (Next.js) image. Build context: the frontend/ folder.
#
#   docker build -t slothpay-web \
#     --build-arg BACKEND_URL=https://your-api.onrender.com \
#     frontend
#
# BACKEND_URL is baked in at build time. With it set, the browser calls this site's own /api/...
# and the Next.js server forwards it to the API, so the login cookie is a first-party cookie.
# Leave it empty only if the browser should call the API directly (NEXT_PUBLIC_API_BASE_URL).

FROM node:22-slim AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM node:22-slim AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .

# Build-time values. NEXT_PUBLIC_* ends up in the browser bundle: never put a secret in it.
# An empty NEXT_PUBLIC_API_BASE_URL means "same origin" (the proxy above).
ARG BACKEND_URL=""
ARG NEXT_PUBLIC_API_BASE_URL=""
ARG NEXT_PUBLIC_USE_MOCKS="false"
ARG NEXT_PUBLIC_ADMIN_API_KEY=""
ENV BACKEND_URL=$BACKEND_URL \
    NEXT_PUBLIC_API_BASE_URL=$NEXT_PUBLIC_API_BASE_URL \
    NEXT_PUBLIC_USE_MOCKS=$NEXT_PUBLIC_USE_MOCKS \
    NEXT_PUBLIC_ADMIN_API_KEY=$NEXT_PUBLIC_ADMIN_API_KEY
RUN npm run build

FROM node:22-slim AS run
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

# The standalone output holds the server and only the files it needs. This app has no public/
# folder, so only the static assets are copied next to it.
COPY --from=build --chown=node:node /app/.next/standalone ./
COPY --from=build --chown=node:node /app/.next/static ./.next/static

USER node
EXPOSE 3000
CMD ["node", "server.js"]