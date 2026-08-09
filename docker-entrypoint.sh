#!/bin/sh
# Fix bind-mount ownership so the non-root app user can write DATA_DIR.
set -e
DATA_DIR="${DATA_DIR:-/data}"
if [ "$(id -u)" = "0" ]; then
  mkdir -p "$DATA_DIR"
  # Only fix ownership of the state dirs we need; avoid walking multi-TB content.
  if ! chown app:app "$DATA_DIR" 2>/dev/null; then
    echo "warning: could not chown $DATA_DIR (bind mount may reject writes by app user)" >&2
  fi
  for sub in content torrents resume; do
    mkdir -p "$DATA_DIR/$sub"
    chown app:app "$DATA_DIR/$sub" 2>/dev/null || true
  done
  # settings.json / resume files / leftover .torrent metadata created as root on first boot
  chown app:app "$DATA_DIR"/settings.json 2>/dev/null || true
  chown -R app:app "$DATA_DIR/torrents" "$DATA_DIR/resume" 2>/dev/null || true
  # Nested incomplete downloads left root-owned by older images must be writable.
  # ponytail: full tree walk; skip when CONTENT_CHOWN=0 for huge binds.
  if [ "${CONTENT_CHOWN:-1}" != "0" ]; then
    chown -R app:app "$DATA_DIR/content" 2>/dev/null || true
  else
    chown app:app "$DATA_DIR/content" 2>/dev/null || true
  fi
  if ! runuser -u app -- test -w "$DATA_DIR" \
    || ! runuser -u app -- test -w "$DATA_DIR/content" \
    || ! runuser -u app -- test -w "$DATA_DIR/torrents" \
    || ! runuser -u app -- test -w "$DATA_DIR/resume"; then
    echo "error: app user cannot write $DATA_DIR (fix bind-mount ownership or permissions)" >&2
    exit 1
  fi
  exec runuser -u app -- "$@"
fi
exec "$@"
