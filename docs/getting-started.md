# Getting started

GameFinder sits between three things I already had running:

1. Prowlarr searches the game/console indexers.
2. qBittorrent downloads the result into a ROM/game category.
3. GameFinder stages the finished payload into the RomM library folder by hardlinking when possible, or copying when hardlinks are not available.

## Basic flow

```text
Browser -> GameFinder -> Prowlarr search
Browser -> GameFinder -> qBittorrent add
qBittorrent complete -> GameFinder jobs page -> RomM library folder
```

## What you need

- Prowlarr with at least one game/console-capable indexer.
- qBittorrent with the Web API reachable from the GameFinder container.
- A path for incoming downloads.
- A path for the final RomM-style library.
- Optional: RomM database/resources if you want homepage widgets and cover thumbnails.

## Setup checklist

1. Copy `.env.example` to `.env`.
2. Set `PROWLARR_URL` and `PROWLARR_API_KEY`.
3. Set `GAMEFINDER_INDEXER_IDS` to the Prowlarr indexers you actually want searched.
4. Set `QBIT_URL` to the qBittorrent Web API endpoint GameFinder can reach.
5. Set `QBIT_SAVE_PATH` and `ROMS_HOST_PATH` so both containers are looking at the same files.
6. Set a real `SECRET_KEY`.
7. Start it with Docker Compose.
8. Open `/health`, then `/`, and try a small search.

If search works but staging fails, check paths first. Almost every annoying setup issue is qBittorrent reporting a path from its own container that GameFinder cannot see.
