# Debian base so the apt python3-libtorrent matches the system python3.
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    TORRENT_PORT=6881 \
    FRONTEND_DIR=/app/frontend

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-libtorrent \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Web dependencies installed into the system interpreter (which also sees
# python3-libtorrent in dist-packages). --break-system-packages is required on
# Debian bookworm's externally-managed environment.
COPY backend/requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /app/requirements.txt

COPY backend /app/backend
COPY frontend /app/frontend

VOLUME ["/data"]

# Web UI
EXPOSE 8080
# BitTorrent (TCP + uTP/DHT over UDP)
EXPOSE 6881/tcp
EXPOSE 6881/udp

WORKDIR /app/backend
CMD ["python3", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
