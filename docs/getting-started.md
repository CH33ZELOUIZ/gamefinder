# Getting started

GameFinder is a small Flask app that connects three pieces of a game-library workflow:

1. Prowlarr searches game/console-capable indexers.
2. qBittorrent downloads selected results into a ROM/game category.
3. A staging job hardlinks or copies completed payloads into a RomM-style library folder.

## Basic flow

```text
Browser -> GameFinder -> Prowlarr search
Browser -> GameFinder -> qBittorrent add
qBittorrent complete -> GameFinder job page -> library stage folder
```

## Required services

- Prowlarr with at least one game/console-capable indexer.
- qBittorrent Web API.
- A filesystem location for incoming downloads.
- A filesystem location for the final library.
- Optional: RomM database/resources for landing-page widgets and thumbnails.

## Setup checklist

1. Copy `.env.example` to `.env`.
2. Set Prowlarr URL/API key.
3. Set qBittorrent URL and credentials if needed.
4. Set incoming and final library paths using the container path names.
5. Start with Docker Compose.
6. Open `/health` and `/` to confirm the app is ready.
