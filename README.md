# annas-torrents-webui

A web interface for [**annas-torrents**](https://github.com/cparthiv/annas-torrents) — turning the CLI-only tool for seeding [Anna's Archive](https://annas-archive.pk) into a dashboard for selecting, seeding, monitoring, and sharing a contribution from your browser.

> Anna's Archive is the largest open library in human history. It stays alive because volunteers seed its torrents. This project makes it easy to **choose how much you contribute, watch what you're actually sharing, and show it off.**

![Anna's Torrents Seedbox dashboard — contribution target, archive coverage, and live upload/download/disk/peer metrics](doc/screenshot1.png)

---

## Why a Web UI?

The original `annas-torrents` is a command-line script: you run `python main.py`, type how many terabytes you want to target, and it downloads the matching `.torrent` metadata files into a `/torrents` folder. You then load those into a BitTorrent client yourself and have little visibility into what is happening.

That works, but it is opaque. Once the torrents are seeding you have no easy view of:

- How much of Anna's Archive you're actually preserving
- How much disk you've committed vs. how much is left
- Whether you're uploading, and how fast
- How many peers are relying on your copy
- Which torrents can be removed safely when space is tight

This web UI wraps the same torrent-selection logic in a dashboard that **parametrizes the contribution, validates metadata, manages either supported torrent backend, surfaces live metrics, and lets you share your impact.**

---

## Features

### Parametrize your contribution
- Set **Content to add** in decimal TB/GB units (up to `30 TB`; e.g. `0.05 TB` = `50.00 GB`). This is how much torrent *content* to select and add, not a promised upload amount.
- One click fetches the prioritized torrent list from Anna's Archive and starts seeding with embedded **libtorrent** or an existing **qBittorrent** instance.
- The selection flow falls back between trusted Anna's Archive HTTPS mirrors, downloads metadata with size limits, validates bencoded torrent data, and verifies the infohash before adding it.
- When the requested amount is reached, the app **stops adding** and keeps seeding what you already have.
- **I need more space** previews a deletion plan for the selected destination. It prefers large, well-seeded torrents, uses allocated/on-disk bytes for incomplete torrents (falling back to downloaded progress when allocation is unknown), avoids unknown-seed torrents, and treats torrents under `10.00 GB` as a soft keep preference.
- Torrent removal requires explicit confirmation, validates the infohash, stays inside the torrent's save path, and reports when metadata was removed but content files could not be deleted.
- Global **Pause / Resume** controls are available separately for seeding and downloading. Upload and download limits support presets and custom Mbps values.

### See what you're actually sharing
- **Archive coverage** — how much of Anna's Archive your indexed torrent content represents, weighted by actual download progress.
- **Storage committed** — bytes allocated by active torrents, plus free space where the backend can report it.
- **Bandwidth** — live upload/download rates and total uploaded/downloaded.
- **Swarm health** — connected and total seeds/peers per torrent when the backend knows them.
- **Download destination** — choose the default path, an allowlisted path, a path returned by the native folder picker, or a path already in use by an active torrent.
- **Disk preallocation** (Advanced) — optionally reserve full file size when adding torrents (libtorrent allocate mode; for qBittorrent, temporarily enables `preallocate_all` during provisioning and restores the previous value afterward).
- Live updates over SSE, with connected, degraded, reconnecting, and offline states.

### Configure and protect the service
- Switch between `libtorrent` and `qbittorrent` from Settings when the image contains the selected backend.
- Configure qBittorrent URL, user, category, and save-path behavior. The qBittorrent password can be changed at runtime but is never written to `settings.json`.
- Settings are validated, saved atomically, and backend changes are rejected while provisioning is running.
- Private API routes require `API_TOKEN` by default. The browser uses a short-lived, one-use SSE ticket instead of putting the long-lived token in the event-stream URL.
- Public status and events are redacted: no filesystem paths, infohashes, torrent names, controls, or settings are exposed.

### Share your impact & support Anna's Archive
- **Share buttons** for X, Bluesky, Mastodon, Reddit, Telegram, WhatsApp, Facebook, LinkedIn, Email, and copy — plus the native share sheet where available. The message uses rounded factual numbers from your contribution.
- With `PUBLIC_URL` set, share links can point to a **read-only `/view` page** showing community impact without the control panel or private torrent details. The server bakes `class="view-mode"` into the HTML so CSS hides private chrome even before JavaScript runs; the public API still serves a redacted snapshot.
- **Donate to Anna's Archive** stays in the header.

---

## How It Works

```
┌────────────────┐   token HTTP + SSE   ┌──────────────────────────────┐
│   Web UI       │◄────────────────────►│  Backend (FastAPI)           │
│   (browser)    │                       │  selection · metrics · auth  │
└────────────────┘                       └───────────┬──────────────────┘
                                                     │
                    ┌────────────────────────────────┴───────────────────┐
                    ▼                                                    ▼
         TORRENT_BACKEND=libtorrent                       TORRENT_BACKEND=qbittorrent
         (embedded session seeds)                         (Web API → your qBittorrent)
```

1. **Choose Content to add** — how much torrent content to select, then optionally choose an Advanced download destination.
2. **Resolve a destination** — the backend accepts only configured roots, selected folders, or destinations already associated with active torrents.
3. **Select** — the backend calls Anna's Archive `generate_torrents`, with mirror fallback and validated request limits.
4. **Download metadata** — torrent URLs remain on trusted HTTPS mirrors; payload size and raw info-dictionary hashes are checked before files are stored under `DATA_DIR/torrents`.
5. **Seed** — either the **embedded libtorrent** session (default), or **your qBittorrent** via Web API. Existing torrents are loaded on startup and resume data is saved during shutdown.
6. **Observe** — live stats and archive coverage are streamed to the UI over authenticated SSE. Public status uses a redacted snapshot.
7. **Share** — optional; uses factual contribution numbers and `/view` when `PUBLIC_URL` is set.

---

## Tech Stack

| Layer     | Choice |
|-----------|--------|
| Frontend  | Single static page with vanilla JavaScript, CSS, fetch, local state, modals, sharing, and SSE |
| Backend   | Python + FastAPI + Pydantic — provisioning, validation, live metrics, coverage, auth, and settings |
| Selection | `httpx` with trusted HTTPS mirror fallback, bounded torrent downloads, bencode/infohash validation |
| Torrent   | **libtorrent** (default, embedded) **or** [qBittorrent](https://www.qbittorrent.org/) Web API |
| Storage   | Atomic `settings.json`, safe destination matching, path-contained deletion, and resume persistence |
| Delivery  | Debian-based Docker image; entrypoint starts as root to fix bind-mount ownership, then drops to non-root `app`; Compose override for libtorrent ports; GitHub Actions CI |
| Tests     | Python `unittest`, module self-checks, frontend JavaScript syntax/ID regression check, and Docker build smoke test |

---

## Getting Started

### Prerequisites
- Docker with Docker Compose v2.
- Disk space for whatever you choose to seed.
- For **libtorrent** (default): a forwarded BitTorrent port if you want incoming peer connections. The web-only Compose file does not publish port `6881`; use the libtorrent override below.
- For **qBittorrent**: Web API enabled, a category for this app, and a password when qBittorrent is outside the app container.
- A long random `API_TOKEN` for any non-local or real deployment. Private APIs are intentionally unavailable by default when the token is missing.

### Run with Docker Compose (libtorrent — default)

Create a local `.env` file. It is ignored by Git:

```env
API_TOKEN=replace-with-a-long-random-value
# PUBLIC_URL=https://seed.example.com
```

Then run:

```bash
git clone https://github.com/worph/annas-torrents-webui
cd annas-torrents-webui
docker compose -f docker-compose.yml -f docker-compose.libtorrent.yml up -d --build
```

Open **`http://localhost:8090`**, set **Content to add** (TB), choose a destination if needed, and click **Start contributing**.

The Compose override publishes `6881` TCP and UDP for libtorrent. The base Compose file publishes the web UI on localhost port **8090** by default (avoids clashing with qBittorrent on 8080) and does not publish the BitTorrent port.

Sizes in the UI use decimal **GB / TB** with two decimals (e.g. `22.03 TB`).

### Run with an existing qBittorrent

Put the qBittorrent settings in `.env`:

```env
API_TOKEN=replace-with-a-long-random-value
TORRENT_BACKEND=qbittorrent
QBIT_URL=http://host.docker.internal:8080
QBIT_USER=admin
QBIT_PASS=change-me
QBIT_CATEGORY=Anna's Archive Torrents
# QBIT_SAVE_PATH=/path/as-seen-by-qBittorrent
# WEB_PORT defaults to 8090 (this app); qBittorrent stays on 8080
```

Then run only the base Compose file:

```bash
docker compose up -d --build
```

The dashboard imports torrents already in the configured category and can provision new ones into it. qBittorrent paths are interpreted by qBittorrent, not by the app container. Keep `QBIT_PASS` in the environment unless qBittorrent explicitly allows unauthenticated localhost clients.

The Docker build installs `python3-libtorrent` only when the build argument `TORRENT_BACKEND=libtorrent` (the default). That same argument becomes the image's default runtime `TORRENT_BACKEND`. If you want to switch from a qBittorrent-only image to embedded libtorrent, rebuild with `TORRENT_BACKEND=libtorrent`; changing the setting in the UI cannot install a missing system package.

### Download destination

The **Download destination** selector chooses where *new* torrents are saved. Existing torrents are not moved.

| Backend | Default path | Other locations |
|---------|--------------|-----------------|
| libtorrent | `DATA_DIR/content` (`./data/content` next to the repo / Compose volume) | **Browse…** in the UI, or `STORAGE_PATHS` (the path must be mounted into the container) |
| qBittorrent | `QBIT_SAVE_PATH` when set; otherwise no save path is sent and qBittorrent uses its own default | `STORAGE_PATHS` using paths as qBittorrent sees them (**Browse…** is libtorrent-only) |

`./data` is the app's state volume: torrent metadata, resume data, settings, and the default libtorrent content directory. Extra libtorrent destinations must be mounted, for example:

```yaml
volumes:
  - ./data:/data
  - /mnt/bigdisk:/extra
```

```env
STORAGE_PATHS=/data/content,/extra
```

On Windows with **libtorrent**, other local drives appear as `D:` / `E:` and resolve to `X:\Anna's Archive Torrents`, created if missing. **Browse…** opens a native folder dialog only for the embedded libtorrent backend when it runs directly on Windows; Docker and qBittorrent deployments should use `STORAGE_PATHS` and volume mounts. The dialog opens on the machine running the backend.

The app does not accept arbitrary save paths. A torrent matches a destination only when its save path equals or is inside that destination (selecting a child folder does not match torrents stored in the parent). Space recovery preview uses the same destination allowlist as provisioning. Path checks keep qBittorrent's remote paths separate from local disk accounting, and refuse deletion targets that escape the torrent's save directory.

### API authentication

The private API is protected by `API_TOKEN` by default. Send it as either header:

```bash
curl -H "X-API-Token: $API_TOKEN" http://localhost:8090/api/status
curl -H "Authorization: Bearer $API_TOKEN" http://localhost:8090/api/status
```

The browser stores the token locally and uses it for private requests. For `/api/events`, it first requests `/api/events/ticket`; the server returns a one-use ticket valid for 60 seconds, so the long-lived API token is not placed in the SSE URL.

These endpoints are intentionally public and redacted where applicable:

- `/api/public/config`
- `/api/public/status`
- `/api/public/events`
- `/view` and the static frontend

`/api/healthz` is a public liveness probe (process up). `/api/health` is a readiness endpoint: it reports backend and authentication readiness and returns `503` when either is not ready. The container HEALTHCHECK uses `/api/healthz` so an empty `API_TOKEN` does not mark the container unhealthy forever.

For a trusted local development instance without a token, set `ALLOW_UNAUTHENTICATED_API=1`. Do not use that setting on an exposed service.

### Ports & volume

| What            | Value |
|-----------------|-------|
| Web UI          | `127.0.0.1:8090` (override with `WEB_PORT`; localhost-bound by default) |
| BitTorrent      | `6881` TCP+UDP with `docker-compose.libtorrent.yml`; not published by the base Compose file |
| Data volume     | `./data` → `/data` — app state; default libtorrent content is `/data/content` |
| Health          | `GET /api/healthz` (public liveness); `GET /api/health` (private readiness) |

### Configuration (environment variables)

| Variable | Default | Description |
|----------|---------|-------------|
| `WEB_PORT` | `8090` | Host port used by Compose for the web UI (avoids clashing with qBittorrent on 8080) |
| `HOST_DATA_DIR` | `./data` | Host path bind-mounted into the container at `DATA_DIR` |
| `TORRENT_BACKEND` | `libtorrent` | `libtorrent` or `qbittorrent`; Docker build arg installs packages and sets the image default |
| `TORRENT_PORT` | `6881` | libtorrent listen port |
| `DATA_DIR` | `/data` | App state: torrent metadata, resume data, and `settings.json`; default content is `DATA_DIR/content` |
| `STORAGE_PATHS` | *(empty)* | Extra allowlisted save paths, comma- or semicolon-separated |
| `QBIT_URL` | `http://host.docker.internal:8080` in Compose | qBittorrent Web API base URL |
| `QBIT_USER` | `admin` | qBittorrent Web API username |
| `QBIT_PASS` | *(empty)* | qBittorrent password; environment-only and never persisted by the app |
| `QBIT_CATEGORY` | `Anna's Archive Torrents` | Category to import/add; UI changes are saved in `DATA_DIR/settings.json` |
| `QBIT_SAVE_PATH` | *(empty)* | Default save path as qBittorrent sees it; empty means use qBittorrent's own default |
| `PUBLIC_URL` | *(unset)* | Public base URL used to create `/view` share links |
| `API_TOKEN` | *(unset)* | Required for private APIs by default; use a long random value |
| `ALLOW_UNAUTHENTICATED_API` | *(unset)* | Development-only escape hatch; `1`, `true`, or `yes` permits private APIs without `API_TOKEN` |
| `CONTENT_CHOWN` | `1` | Entrypoint recursively `chown`s `$DATA_DIR/content`; set `0` to skip the walk on large binds |

Settings changed in the UI are validated before a backend switch, written atomically, and kept in `DATA_DIR/settings.json`. `QBIT_PASS` is deliberately excluded from that file, so it must be supplied again through the environment after a restart.

### Run checks locally

Install the backend dependencies and run the regression suite:

```bash
python3 -m pip install -r backend/requirements.txt
python3 -m unittest discover -s tests -v
```

The frontend JavaScript syntax check needs Node.js (`node --check`). On Windows, `python` usually works as a substitute for `python3`.

Run the module self-checks:

```bash
cd backend
python3 -m app.space
python3 -m app.settings
python3 -m app.storage
python3 -m app.metrics
python3 -m app.pathsafety
cd ..
```

GitHub Actions runs the unit tests, module checks, frontend JavaScript syntax / unique-ID check, Compose config validation, and Docker build + import smokes for both `libtorrent` and `qbittorrent` images on pushes to `main`, version tags, and pull requests targeting `main`. Docker must be available for the smoke steps.

The entrypoint recursively `chown`s `$DATA_DIR/content` so resumed downloads remain writable after older root-owned runs. Set `CONTENT_CHOWN=0` to skip that walk on very large binds (only the top-level content directory is then owned).

---

## Roadmap

- [x] Backend selection module (`generate_torrents`, trusted mirror fallback, metadata validation)
- [x] Embedded libtorrent session for live disk/bandwidth/swarm metrics
- [x] Optional qBittorrent Web API backend (`TORRENT_BACKEND=qbittorrent`)
- [x] Anna's Archive totals → progress-weighted coverage percentage
- [x] Dashboard UI with live metric cards, coverage bar, compact controls, and SSE
- [x] API token authentication, Bearer support, redacted public endpoints, and SSE tickets
- [x] Multi-network sharing + Donate to Anna's Archive
- [x] Read-only vantage page (`/view`)
- [x] Content to add flow, impact summary, global pause/resume, and upload/download limits
- [x] Safe download destinations, Windows folder picker, and Docker storage allowlist
- [x] I need more space (tokenized deletion preview, safe path deletion, protect under 10 GB)
- [x] Atomic settings persistence without storing qBittorrent passwords
- [x] Single Docker image / Compose delivery (entrypoint drops to non-root `app` after ownership fix) with optional libtorrent port publishing
- [x] Regression tests, module self-checks, pull-request CI, Compose validation, and Docker build/import smoke tests
- [ ] Collection filtering in the UI (backend already supports it)
- [ ] Bandwidth/coverage sparklines
- [x] Per-torrent remove with confirmation and file-deletion status
- [x] Disk preallocation

---

## A Note on Safety

This app **downloads and seeds actual content** via libtorrent or qBittorrent. Distributing certain materials may not be legal in all jurisdictions. **Use a VPN** when running it, and seed at your own risk. You are responsible for what you choose to contribute.

Prefer the default localhost binding, set a strong `API_TOKEN`, and use HTTPS if the service is exposed through a reverse proxy. The `/view` page and `/api/public/*` endpoints are read-only and redact paths, hashes, torrent names, controls, and settings. Keep `.env` files and the `data/` volume out of commits.

---

## Credits

- Built on top of [**cparthiv/annas-torrents**](https://github.com/cparthiv/annas-torrents).
- Maintained at [**worph/annas-torrents-webui**](https://github.com/worph/annas-torrents-webui).
- For the benefit of [**Anna's Archive**](https://annas-archive.pk) and everyone who keeps it alive.

## License

See [LICENSE](./LICENSE).
