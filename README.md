# annas-torrents-webui

A web interface for [**annas-torrents**](https://github.com/cparthiv/annas-torrents) — turning the CLI-only tool for seeding [Anna's Archive](https://annas-archive.org) into a friendly dashboard you can drive from your browser.

> Anna's Archive is the largest open library in human history. It stays alive because volunteers seed its torrents. This project makes it easy to **choose how much you contribute, watch what you're actually sharing, and show it off.**

---

## Why a Web UI?

The original `annas-torrents` is a command-line script: you run `python main.py`, type how many terabytes you want to target, and it downloads the matching `.torrent` metadata files into a `/torrents` folder. You then load those into a BitTorrent client (qBittorrent) yourself and hope you set everything up right.

That works, but it's opaque. Once the torrents are seeding you have no easy view of:

- How much of Anna's Archive you're actually preserving
- How much disk you've committed vs. how much is left
- Whether you're uploading, and how fast
- How many peers are relying on your copy

This web UI wraps the same torrent-selection logic in a dashboard that **parametrizes the contribution, surfaces live metrics, and lets you share your impact.**

---

## Features

### 🎛️ Parametrize your contribution
- Set a **target size** (in TB/GB, decimals supported — e.g. `0.05 TB` = 50 GB).
- One click to fetch the prioritized `.torrent` list from Anna's Archive and **start seeding** — no CLI, no separate torrent client to install and wire up.
- Collection filtering (books, papers, comics, metadata) is already supported by the backend and coming to the UI.

### 📊 See what you're actually sharing
- **Coverage** — how much of the total Anna's Archive dataset your seeded torrents represent, shown as a percentage and absolute size.
- **Disk used** — space committed by your active torrents, and headroom remaining on the volume.
- **Bandwidth** — live upload / download rates and cumulative totals.
- **Swarm health** — seeders and leechers per torrent, so you can see who depends on you.
- Per-torrent and aggregate views, refreshed in near real time.

### 📣 Share your impact & support Anna's Archive
- **Share buttons** for X, Bluesky, Mastodon, Reddit, Telegram, WhatsApp, Facebook, LinkedIn, Email, and copy-to-clipboard — plus the native mobile share sheet (Web Share API). The message explains *why* preserving Anna's Archive matters and embeds your live contribution.
- Share links point to a **read-only vantage page (`/view`)** of your live seedbox, so others see your real-time contribution — not your control panel.
- A **❤️ Donate to Anna's Archive** button linking straight to their donation page.

---

## How It Works

```
┌────────────────┐   HTTP + SSE   ┌──────────────────────────────────┐
│                │◄──────────────►│  Backend (FastAPI)               │
│   Web UI       │                │  ┌────────────────────────────┐  │
│   (browser)    │                │  │ embedded libtorrent session│──┼──► BitTorrent swarm
│                │                │  └────────────────────────────┘  │    (seeds content)
└────────────────┘                │   selection · metrics · coverage │
                                  └───────────────┬──────────────────┘
                                                  └──► Anna's Archive
                                                       (torrent list + totals)
```

1. **Parametrize** — you set target size (and later, collections) in the UI.
2. **Select** — the backend calls Anna's Archive `generate_torrents` (server-side prioritized), downloads the matching `.torrent` files to the data volume.
3. **Seed** — each torrent is added to the **embedded libtorrent session**, which downloads and seeds the actual content.
4. **Observe** — the backend reads live stats straight from the session and joins them against the Anna's Archive index (by `data_size`) for coverage, disk, bandwidth, and swarm metrics, streamed to the UI over SSE.
5. **Share** — the share button turns your live stats into a post.

Unlike the original CLI (which only fetched `.torrent` metadata and left seeding to you), **this app downloads and seeds the content itself** — so your target TB is the real disk it consumes and contributes.

---

## Tech Stack

| Layer     | Choice                                                        |
|-----------|---------------------------------------------------------------|
| Frontend  | Single static page (vanilla JS + SSE) — metric cards, coverage bar, sharing, `/view` vantage page |
| Backend   | Python + FastAPI — provisioning, live metrics, coverage       |
| Torrent   | **Embedded [libtorrent](https://www.libtorrent.org/)** — the app *is* the client, no external client to install |
| Delivery  | Single Docker image / Compose service, no auth                |

The app runs one libtorrent session in-process. It downloads and seeds the
actual content to a data volume, and reads live stats (bandwidth, peers,
seeders) straight from the session — no external client or database.

---

## Getting Started

### Prerequisites
- Docker.
- Disk space for whatever you choose to seed (the target TB is the real disk it will use).
- A forwarded/open port for BitTorrent peer traffic (default `6881`, TCP + UDP) for best connectivity.

### Run with Docker Compose

```bash
git clone https://github.com/cparthiv/annas-torrents-webui
cd annas-torrents-webui
docker compose up -d --build
```

Open the UI at **`http://localhost:8080`**, set a contribution target (TB), and
click **Start contributing**. The app fetches the prioritized torrent list,
begins downloading + seeding to `./data`, and the dashboard updates live.

> ⚠️ **No authentication.** The UI has no login — keep it on a trusted network
> (LAN / VPN / behind a reverse proxy). Don't expose port 8080 to the internet.

To serve the UI on a different host port (e.g. if 8080 is taken):
`WEB_PORT=8090 docker compose up -d`.

### Ports & volume

| What            | Value                                                        |
|-----------------|--------------------------------------------------------------|
| Web UI          | `8080/tcp`                                                   |
| BitTorrent      | `6881/tcp` + `6881/udp` (peer traffic, DHT/uTP)              |
| Data volume     | `./data` → `/data` — `.torrent` files, seeded content, resume state |

### Configuration (environment variables)

| Variable       | Default   | Description                                  |
|----------------|-----------|----------------------------------------------|
| `DATA_DIR`     | `/data`   | Where content, `.torrent` files, and resume data live |
| `TORRENT_PORT` | `6881`    | libtorrent listen port (TCP + UDP)           |
| `PUBLIC_URL`   | *(unset)* | Public base URL for share links, e.g. `https://seed.example.com`. Unset → the browser's origin is used. Set it when behind a reverse proxy or public domain so shared `/view` links resolve correctly. |

State is **live-only**: metrics are in-memory, but the torrent set and
libtorrent resume data persist on the data volume, so restarts resume seeding
without re-checking from scratch.

---

## Roadmap

- [x] Backend selection module (Anna's Archive `generate_torrents`, mirror fallback)
- [x] Embedded libtorrent session for live disk/bandwidth/swarm metrics
- [x] Anna's Archive totals → coverage percentage (by `data_size`)
- [x] Dashboard UI with live metric cards + coverage bar (SSE)
- [x] Multi-network sharing (X, Bluesky, Mastodon, Reddit, Telegram, WhatsApp, Facebook, LinkedIn, Email, copy) + native Web Share API
- [x] Read-only vantage page (`/view`) + Donate to Anna's Archive button
- [x] Single Docker image / Compose delivery
- [ ] Collection filtering in the UI (backend already supports it via `top_level_group`/`group`)
- [ ] Bandwidth/coverage sparklines (in-memory history)
- [ ] Per-torrent controls (pause/remove) and global up/down rate limits

---

## A Note on Safety

This app **downloads and seeds actual content**. Distributing certain materials may not be legal in all jurisdictions. **Use a VPN** when running it, and seed at your own risk. You are responsible for what you choose to contribute. The UI ships with **no authentication** — do not expose it to the public internet.

---

## Credits

- Built on top of [**cparthiv/annas-torrents**](https://github.com/cparthiv/annas-torrents).
- For the benefit of [**Anna's Archive**](https://annas-archive.org) and everyone who keeps it alive.

## License

See [LICENSE](./LICENSE).
