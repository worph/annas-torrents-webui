# Changelog

All notable changes to this project are documented here. Releases should use
annotated Git tags (`vX.Y.Z`) and GitHub Release notes that match this file.

The GHCR image `ghcr.io/worph/annas-torrents-webui:latest` tracks `main` and
version tags — **pin a semver tag** for production, not `latest`.

## Unreleased

### Bug fixes (deep hunt)
- Libtorrent empty `remove_torrents` no longer claims `files_deleted: true`.
- Frontend: Settings/reconnect no longer leaves provision button stuck (`provisionInFlight`); space free / remove modal recover from interrupted requests.
- CI: container smoke with required `API_TOKEN`; Playwright checks SSE ticket one-shot.

### Security / trust
- Public `/view` snapshot no longer includes host disk capacity fields.
- Minimal CSP and browser hardening headers on responses.
- README “Exposing safely” checklist.

### Honesty / ops
- Provision refuses unknown free space unless `allow_unknown_disk` is confirmed.
- qBittorrent removals never report `files_deleted: true` without verification.
- Space planner considers best triples to reduce overshoot; preview warns more clearly.
- Document native Windows delete TOCTOU residual.

### Onboarding
- GHCR pull quickstart, Windows Docker notes, `.env.example`, entrypoint chown/token messages.

### Release / community
- Playwright e2e runs with required `API_TOKEN`.
- CONTRIBUTING, SECURITY, CoC, issue/PR templates.
- Libtorrent rates decay to 0 after consecutive `session_stats` misses.
- Optional `TRUST_PROXY_HEADERS` for public SSE IP caps behind a proxy.
