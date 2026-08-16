FROM node:22-alpine AS builder

WORKDIR /build

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend ./
RUN npm run build


FROM nginx:1.28-alpine AS runtime

LABEL org.opencontainers.image.title="Data Agent 网页端" \
      org.opencontainers.image.description="Data Agent 静态网页与 API 反向代理" \
      org.opencontainers.image.source="https://github.com/Maple-yhj/NL2SQL" \
      org.opencontainers.image.licenses="MIT"

COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /build/dist /usr/share/nginx/html

EXPOSE 80
