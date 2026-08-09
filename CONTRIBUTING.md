# Contributing

Small, correct changes beat large rewrites. Read [README.md](./README.md) for how the app runs; this file is only for people changing the code.

## What fits

- Bug fixes with a root-cause patch (prefer one guard in a shared helper over N call-site patches).
- Focused features that match existing UX — no framework rewrites, no new dependencies if stdlib or code already here covers it.
- Docs/tests that make the next stranger succeed faster.

Seedbox control paths (auth, destinations, deletes, qBit URL, provision) are trust boundaries: validate at the edge, and never report success (especially file delete / free space) without evidence.

## Local setup

```bash
python -m pip install -r backend/requirements.txt
cp .env.example .env   # set API_TOKEN, or use ALLOW_UNAUTHENTICATED_API=1 for trusted local only
```

Embedded libtorrent needs the system `python3-libtorrent` package (or the Docker image). qBittorrent mode only needs a reachable Web API.

### Run the API (Linux / macOS)

```bash
cd backend
FRONTEND_DIR=../frontend DATA_DIR=../data TORRENT_PORT=0 ALLOW_UNAUTHENTICATED_API=1 \
  python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8090
```

### Run the API (Windows PowerShell)

```powershell
cd backend
$env:FRONTEND_DIR = "..\frontend"
$env:DATA_DIR = "..\data"
$env:TORRENT_PORT = "0"
$env:ALLOW_UNAUTHENTICATED_API = "1"
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8090
```

Open `http://127.0.0.1:8090`.

## Tests

Non-trivial PRs should leave something that fails if the change regresses. Match what [GitHub Actions](.github/workflows/publish.yml) runs when you can:

```bash
# From repo root
python -m unittest discover -s tests -v

cd backend
python -m app.space
python -m app.settings
python -m app.storage
python -m app.metrics
python -m app.pathsafety
cd ..

node --test tests/frontend/*.mjs
node --check frontend/app.js

# DOM smoke (first time: npm ci && npx playwright install chromium)
npx playwright test
```

Playwright starts the app with a required `API_TOKEN` (see `playwright.config.mjs`). Do not “fix” e2e by setting `ALLOW_UNAUTHENTICATED_API=1` in that config.

## Pull requests

- Prefer one concern per PR.
- Describe *why*; link an issue when there is one.
- User-visible behavior → note under **Unreleased** in [CHANGELOG.md](./CHANGELOG.md).
- Never commit `.env`, tokens, or `data/` content.

By participating you agree to the [Code of Conduct](./CODE_OF_CONDUCT.md). Security bugs: [SECURITY.md](./SECURITY.md), not a public issue.
