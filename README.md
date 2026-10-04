# GameFinder

GameFinder is a little Flask app I built for my homelab so I could search game-friendly Prowlarr indexers, send a result to qBittorrent, and have the finished download show up in my RomM library without breaking seeding.

It is not a full download manager and it is not trying to replace RomM. It is just the missing glue between Prowlarr, qBittorrent, and a RomM-style `/roms/<platform>/roms` folder layout.

## What it does

- Searches only the Prowlarr indexers/categories you point it at, instead of hammering every movie/TV/general indexer.
- Lets you pick a platform/console and filters out obvious non-game results.
- Sends magnets or torrent files to qBittorrent under a dedicated category.
- Shows transfer/staging progress on the `/jobs` page.
- Hardlinks completed payloads into `/roms/<platform>/roms` when it can, and copies when it has to.
- Leaves qBittorrent's original payload alone so the torrent can keep seeding.
- Can read RomM database/artwork paths for homepage widgets and thumbnails if you wire that up.

## Safety and compliance notes

This is just glue for services you already run. You are responsible for your own indexers, tracker/API rules, and what you download. Do not commit real API keys, passwords, private tracker names, or paths that expose your setup.

## Quick start

```bash
git clone https://github.com/<your-user>/gamefinder.git
cd gamefinder
cp .env.example .env
# edit .env with your own Prowlarr/qBittorrent/RomM details
docker compose up -d --build
```

Then open <http://localhost:3020>.

## Required configuration

| Variable | Purpose |
| --- | --- |
| `PROWLARR_URL` | Base URL for Prowlarr. |
| `PROWLARR_API_KEY` | Prowlarr API key. Keep this secret. |
| `GAMEFINDER_INDEXER_IDS` | Comma-separated Prowlarr indexer IDs to search. Use game/console-capable indexers here. |
| `QBIT_URL` | qBittorrent WebUI/API URL. |
| `QBIT_CATEGORY` | qBittorrent category for GameFinder downloads. Defaults to `roms`. |
| `QBIT_SAVE_PATH` | Save path qBittorrent should use for incoming ROM/game downloads. |
| `ROMS_HOST_PATH` | Host path mounted into the GameFinder container as `/roms`. |
| `SECRET_KEY` | Flask session secret. Set this to a long random value. |

Optional RomM DB settings (`DB_HOST`, `DB_NAME`, `DB_USER`, `DB_PASSWD`) power the homepage widgets. GameFinder still works without them.

Optional basic auth is available with `GAMEFINDER_USER` and `GAMEFINDER_PASSWORD`. Leave them blank only if this is safely behind your own LAN/auth proxy.

## Path mapping

The one thing to get right is paths. qBittorrent may report paths from its own container, while GameFinder only sees what is mounted at `/roms`.

If qBittorrent says a file lives under a path GameFinder cannot see, set `QBIT_CONTAINER_ROOT` to the qBittorrent-side root that maps to GameFinder's `/roms` mount.

Example:

```env
QBIT_SAVE_PATH=/downloads/roms/_incoming
QBIT_CONTAINER_ROOT=/downloads/roms
ROMS_HOST_PATH=/srv/roms
```

With that setup, qBittorrent can download to `/downloads/roms/_incoming`, while GameFinder sees the same files under `/roms/_incoming`.

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
