# Security Policy

## Supported versions

Security fixes go to `main` and ship in GHCR tags (`ghcr.io/worph/annas-torrents-webui`). Prefer a **pinned semver tag** over `latest` (every push to `main` can move `latest`).

There is no long-term support branch: upgrade the image/tag when a fix matters to you.

## What counts as a security issue

High impact for this project includes, for example:

- Bypassing or weakening `API_TOKEN` / private API auth
- Path traversal or deletes outside an allowlisted save path
- SSRF or unsafe use of the qBittorrent Web API URL
- XSS or other issues that can steal the browser-stored token and drive provision/delete
- Leaking private paths, infohashes, or host disk capacity via `/view` or `/api/public/*` beyond the documented public surface

Default installs bind the UI to localhost, but operators also expose the service (LAN, reverse proxy, `PUBLIC_URL`, mis-set `0.0.0.0`). Treat vulns as if some installs are reachable.

Not a security report: wrong torrent selection, UI polish, Compose docs, or “content legality” — use a normal [bug report](./.github/ISSUE_TEMPLATE/bug_report.yml).

## How to report

**Do not open a public GitHub issue** for security bugs (that publishes the exploit while users are still unpatched).

Prefer, in order:

1. [GitHub private vulnerability report](https://github.com/worph/annas-torrents-webui/security/advisories/new) if the button is available on this repo  
2. Otherwise email the maintainer using the address on the [worph GitHub profile](https://github.com/worph)

Include:

- image tag or git commit
- backend (`libtorrent` / `qbittorrent`) and how you run it (Compose, Desktop, native)
- steps to reproduce (localhost is fine)
- impact (auth bypass, data loss, remote reachability, etc.)

Please give a reasonable window to ship a fix before any public write-up. We will confirm when we can.

## Non-security bugs

Use GitHub Issues (bug or feature templates). Redact tokens and paths you care about in logs.
