# GameFinder

A small self-hosted Flask app that searches console/ROM-capable Prowlarr indexers,
sends selected results to qBittorrent, and stages completed downloads into a RomM-style
library layout while leaving the original torrent payload in place for seeding.

## What it does

- Searches Prowlarr with console/ROM category scoping instead of blasting every indexer.
- Lets you choose a platform/console and filters out movies, TV, music, books, and other non-game categories.
- Sends magnets or torrent files to qBittorrent under a dedicated category.
- Polls qBittorrent job progress from a `/jobs` page.
- Hardlinks completed payloads into `/roms/<platform>/roms` when possible, falling back to copy when needed.
- Optionally reads RomM metadata/resources to render recommendation widgets and thumbnails on the landing page.

## Safety and compliance notes

This project is just glue between services you run. You are responsible for configuring legal indexers,
obeying tracker/API rules, and downloading only content you are allowed to use. Do not commit API keys,
passwords, private tracker details, or host-specific paths.

## Quick start

```bash
git clone https://github.com/<your-user>/gamefinder.git
cd gamefinder
cp .env.example .env
# edit .env with your own Prowlarr/qBittorrent/RomM details
docker compose up -d --build
```

Open <http://localhost:3020>.

## Required configuration

| Variable | Purpose |
| --- | --- |
| `PROWLARR_URL` | Base URL for Prowlarr. |
| `PROWLARR_API_KEY` | Prowlarr API key. Keep this secret. |
| `GAMEFINDER_INDEXER_IDS` | Comma-separated Prowlarr indexer IDs to query. Use only game/console-capable indexers. |
| `QBIT_URL` | qBittorrent WebUI/API URL. |
| `QBIT_CATEGORY` | qBittorrent category for GameFinder downloads, default `roms`. |
| `QBIT_SAVE_PATH` | qBittorrent-side save path for incoming ROM downloads. |
| `ROMS_HOST_PATH` | Host path mounted into the container as `/roms`. |
| `SECRET_KEY` | Flask secret key for sessions. Set a long random value. |

Optional RomM DB settings (`DB_HOST`, `DB_NAME`, `DB_USER`, `DB_PASSWD`) enable landing-page widgets.
The app still works without them.

## Path mapping

qBittorrent may report paths from its own container namespace. If those paths do not exist inside the
GameFinder container, set `QBIT_CONTAINER_ROOT` to the qBittorrent-side root that corresponds to
GameFinder's `/roms` mount.

Example:

```env
QBIT_SAVE_PATH=/downloads/roms/_incoming
QBIT_CONTAINER_ROOT=/downloads/roms
ROMS_HOST_PATH=/srv/roms
```

## Development

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m py_compile app.py
PORT=3020 python app.py
```

## License

MIT
