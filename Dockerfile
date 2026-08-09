# Debian base so the apt python3-libtorrent matches the system python3.
# TORRENT_BACKEND=libtorrent uses in-process seeding; qbittorrent talks to an external client.
FROM debian:bookworm-slim

ARG TORRENT_BACKEND=libtorrent

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    TORRENT_PORT=6881 \
    TORRENT_BACKEND=${TORRENT_BACKEND} \
    FRONTEND_DIR=/app/frontend

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        ca-certificates \
    && if [ "$TORRENT_BACKEND" = "libtorrent" ]; then \
         apt-get install -y --no-install-recommends python3-libtorrent; \
       fi \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system app \
    && useradd --system --gid app --home-dir /app --no-create-home app

WORKDIR /app

COPY backend/requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /app/requirements.txt

COPY --chown=app:app backend /app/backend
COPY --chown=app:app frontend /app/frontend
COPY docker-entrypoint.sh /entrypoint.sh
RUN chmod 755 /entrypoint.sh \
    && mkdir -p /data && chown app:app /data

VOLUME ["/data"]
# Entrypoint runs as root briefly to chown bind-mounted /data, then drops to app.
USER root

EXPOSE 8080
EXPOSE 6881/tcp
EXPOSE 6881/udp

# Public liveness (/api/healthz). Readiness (/api/health) still requires auth when configured.
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
  CMD python3 -c "import urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8080/api/healthz', timeout=3); raise SystemExit(0 if r.status==200 else 1)"

WORKDIR /app/backend
ENTRYPOINT ["/entrypoint.sh"]
CMD ["python3", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
