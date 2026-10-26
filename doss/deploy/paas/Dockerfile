# Doss · VLESS + WebSocket для бесплатных PaaS (Render, Railway, Koyeb, Fly…)
# Образ ~40 МБ, без root-сервисов, только xray + entrypoint.
FROM debian:12-slim

ARG XRAY_VERSION=latest
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl unzip jq python3 \
 && rm -rf /var/lib/apt/lists/*

# архитектура образа → нужный архив Xray
RUN set -eux; \
    case "$(dpkg --print-architecture)" in \
      amd64) ARCH=64 ;; \
      arm64) ARCH=arm64-v8a ;; \
      armhf) ARCH=arm32-v7a ;; \
      *) echo "неизвестная архитектура"; exit 1 ;; \
    esac; \
    if [ "$XRAY_VERSION" = "latest" ]; then \
      URL="https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-${ARCH}.zip"; \
    else \
      URL="https://github.com/XTLS/Xray-core/releases/download/${XRAY_VERSION}/Xray-linux-${ARCH}.zip"; \
    fi; \
    curl -fsSL -o /tmp/xray.zip "$URL"; \
    unzip -oq /tmp/xray.zip -d /tmp/xray; \
    install -m 0755 /tmp/xray/xray /usr/local/bin/xray; \
    mkdir -p /usr/local/share/xray; \
    install -m 0644 /tmp/xray/geoip.dat /usr/local/share/xray/ 2>/dev/null || true; \
    install -m 0644 /tmp/xray/geosite.dat /usr/local/share/xray/ 2>/dev/null || true; \
    rm -rf /tmp/xray /tmp/xray.zip; \
    /usr/local/bin/xray version | head -1

ENV XRAY_LOCATION_ASSET=/usr/local/share/xray
COPY entrypoint.sh /usr/local/bin/doss-entrypoint
RUN chmod 0755 /usr/local/bin/doss-entrypoint

# PaaS сам подставит PORT; локально по умолчанию 8080
ENV PORT=8080
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD jq -e . /tmp/xray-config.json >/dev/null || exit 1

CMD ["/usr/local/bin/doss-entrypoint"]
